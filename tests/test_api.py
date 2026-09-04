"""Tests for the HTTP layer. Offline: the Tiger clients are never built.

The library is covered by its own suite. What matters here is the part the
HTTP layer adds and the CLI does not have: the API key, the two-step token
flow, and the translation of the interactive controls into request fields.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.errors import classify_exception  # noqa: E402
from api.quote_check import check_for_decimal_slip, check_ordinary_rules  # noqa: E402
from api.routes.market import describe_age  # noqa: E402
from api.tokens import PreviewTokenStore, TokenExpired, TokenNotFound  # noqa: E402
from tiger_backend.contracts import (  # noqa: E402
    ExpiredContractError,
    ExpiryNotListedError,
    StrikeNotFoundError,
)
from tiger_backend.market import LastTrade  # noqa: E402
from tiger_backend.orders import OrderSubmissionError  # noqa: E402
from tiger_backend.safety import LiveTradingBlocked  # noqa: E402


class StubQuote:
    """Stands in for a QuoteInput."""

    def __init__(self, bid=0.06, ask=0.09, limit_price=0.09):
        self.bid = bid
        self.ask = ask
        self.limit_price = limit_price
        self.volume = 800
        self.open_interest = 5200


def make_intent_fields(expected_cash=9.0):
    """Minimum fields the token store needs."""
    return dict(
        contract=None,
        quote=None,
        estimate=None,
        action="BUY",
        quantity=1,
        expected_cash=expected_cash,
        take_profit_price=None,
        stop_loss_price=None,
        leg_time_in_force="DAY",
        underlying_price=None,
        cash_available=None,
    )


class TestPreviewTokens:
    """The two-step flow's guarantee lives here."""

    def test_a_token_can_be_redeemed_once(self):
        store = PreviewTokenStore(ttl_seconds=60)
        token, _intent = store.issue(**make_intent_fields())
        redeemed = store.redeem(token)
        assert redeemed.expected_cash == 9.0

    def test_a_spent_token_is_refused(self):
        """The retry case. A client that times out and resends gets nothing."""
        store = PreviewTokenStore(ttl_seconds=60)
        token, _intent = store.issue(**make_intent_fields())
        store.redeem(token)

        with pytest.raises(TokenNotFound):
            store.redeem(token)

    def test_an_unknown_token_is_refused(self):
        store = PreviewTokenStore(ttl_seconds=60)
        with pytest.raises(TokenNotFound):
            store.redeem("never-issued")

    def test_an_expired_token_is_refused_and_consumed(self):
        store = PreviewTokenStore(ttl_seconds=60)
        token, intent = store.issue(**make_intent_fields())

        # Force it past its expiry without waiting a minute.
        store._intents[token] = type(intent)(
            **{
                **{f: getattr(intent, f) for f in intent.__dataclass_fields__},
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            }
        )

        with pytest.raises(TokenExpired):
            store.redeem(token)

        # Consumed even though it failed, so a retry cannot succeed later.
        with pytest.raises(TokenNotFound):
            store.redeem(token)

    def test_concurrent_redeems_yield_exactly_one_winner(self):
        """Two simultaneous submits of one token must not both place an order."""
        store = PreviewTokenStore(ttl_seconds=60)
        token, _intent = store.issue(**make_intent_fields())

        successes = []
        failures = []

        def attempt():
            try:
                store.redeem(token)
                successes.append(True)
            except (TokenNotFound, TokenExpired):
                failures.append(True)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(successes) == 1
        assert len(failures) == 7

    def test_tokens_are_not_guessable(self):
        store = PreviewTokenStore(ttl_seconds=60)
        first, _ = store.issue(**make_intent_fields())
        second, _ = store.issue(**make_intent_fields())
        assert first != second
        assert len(first) > 30


class TestQuoteChecks:
    """The Phase 4 checks, with 400s instead of re-prompts."""

    def test_a_valid_quote_passes(self):
        check_ordinary_rules(StubQuote())

    def test_bid_above_ask_is_rejected(self):
        from api.errors import ApiError

        with pytest.raises(ApiError) as raised:
            check_ordinary_rules(StubQuote(bid=0.20, ask=0.10))

        assert raised.value.status_code == 400
        assert raised.value.error_code == "QUOTE_REJECTED"
        assert "failed_checks" in raised.value.detail

    def test_a_decimal_slip_is_blocked_without_an_override(self):
        from api.errors import ApiError

        last_trade = LastTrade(close=0.07, trade_date=datetime.now().date(), days_old=0)

        with pytest.raises(ApiError) as raised:
            check_for_decimal_slip(2.50, last_trade, override_confirmed=False, multiplier=100)

        error = raised.value
        assert error.status_code == 400
        assert error.error_code == "PRICE_LOOKS_LIKE_DECIMAL_SLIP"
        # The evidence a human needs to judge it.
        for key in ("typed_value", "last_close", "ratio", "last_close_age_days"):
            assert key in error.detail

    def test_the_override_clears_it(self):
        last_trade = LastTrade(close=0.07, trade_date=datetime.now().date(), days_old=0)
        check_for_decimal_slip(2.50, last_trade, override_confirmed=True, multiplier=100)

    def test_no_history_means_the_check_cannot_run(self):
        """Not the same as passing. The preview says so separately."""
        check_for_decimal_slip(2.50, None, override_confirmed=False, multiplier=100)

    def test_a_normal_price_is_not_blocked(self):
        last_trade = LastTrade(close=0.07, trade_date=datetime.now().date(), days_old=0)
        check_for_decimal_slip(0.09, last_trade, override_confirmed=False, multiplier=100)


class TestErrorMapping:
    """Distinct exceptions must get distinct statuses AND codes."""

    def test_expired_expiry_is_gone_not_not_found(self):
        error = classify_exception(ExpiredContractError("expired"))
        assert error.status_code == 410
        assert error.error_code == "EXPIRY_EXPIRED"

    def test_unlisted_expiry_is_not_found(self):
        error = classify_exception(ExpiryNotListedError("nope"))
        assert error.status_code == 404
        assert error.error_code == "EXPIRY_NOT_LISTED"

    def test_expired_and_unlisted_are_distinguishable_without_reading_text(self):
        expired = classify_exception(ExpiredContractError("x"))
        unlisted = classify_exception(ExpiryNotListedError("x"))
        assert expired.status_code != unlisted.status_code
        assert expired.error_code != unlisted.error_code

    def test_bad_strike_is_unprocessable(self):
        error = classify_exception(StrikeNotFoundError("no strike"))
        assert error.status_code == 422
        assert error.error_code == "STRIKE_NOT_FOUND"

    def test_safety_lock_is_forbidden(self):
        error = classify_exception(LiveTradingBlocked("DRY_RUN is true."))
        assert error.status_code == 403

    def test_broker_refusal_is_upstream(self):
        error = classify_exception(OrderSubmissionError("broker said no"))
        assert error.status_code == 502

    def test_entitlement_denial_is_named_as_a_purchase(self):
        error = classify_exception(Exception("code=4 msg=4000:permission denied(US OPT)"))
        assert error.error_code == "UPSTREAM_PERMISSION_DENIED"
        assert "purchase" in error.message

    def test_an_unknown_error_is_still_shaped(self):
        error = classify_exception(ValueError("something odd"))
        assert error.status_code == 500
        assert error.error_code == "INTERNAL_ERROR"


class TestCapabilityAge:
    """A stale probe must not read as current."""

    def test_seconds(self):
        assert "seconds ago" in describe_age(30)

    def test_minutes(self):
        assert "minutes ago" in describe_age(600)

    def test_hours(self):
        assert "hours ago" in describe_age(7200)

    def test_days(self):
        assert "days ago" in describe_age(60 * 60 * 72)


class TestDepsAreNotDeadlocked:
    """The lock in deps.py is reentrant, and this is why.

    get_quote_client holds the lock and then calls get_settings, which takes it
    again on the same thread. With a plain Lock that deadlocks on the first
    real request -- a hang, not a crash, which is far harder to diagnose.
    """

    def test_the_dependency_lock_is_reentrant(self):
        from api import deps

        assert deps._lock.__class__.__name__ == "RLock"

    def test_nested_acquisition_does_not_hang(self):
        from api import deps

        acquired = []

        def nested():
            with deps._lock:
                with deps._lock:
                    acquired.append(True)

        thread = threading.Thread(target=nested)
        thread.start()
        thread.join(timeout=5)

        assert acquired == [True], "nested acquisition deadlocked"


class TestOpenApiSecurityMatchesMiddleware:
    """The schema and the middleware must agree on what is public.

    The Authorize button in /docs is presentation; the middleware is
    enforcement. If they disagree, the docs invite a caller to omit a header
    that is actually required, or imply one is needed where it is not.
    """

    def test_every_route_except_the_unprotected_ones_declares_the_key(self):
        from api.main import UNPROTECTED_PATHS, create_app

        spec = create_app().openapi()

        for path, operations in spec["paths"].items():
            declares_key = any(
                isinstance(operation, dict) and "security" in operation
                for operation in operations.values()
            )
            if path in UNPROTECTED_PATHS:
                assert not declares_key, f"{path} should be public"
            else:
                assert declares_key, f"{path} is enforced but not marked in the schema"

    def test_the_scheme_names_the_header_the_middleware_reads(self):
        from api.main import API_KEY_HEADER, create_app

        spec = create_app().openapi()
        scheme = spec["components"]["securitySchemes"]["ApiKeyAuth"]

        assert scheme["in"] == "header"
        assert scheme["name"] == API_KEY_HEADER
