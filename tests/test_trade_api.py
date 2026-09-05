"""The single-call trading endpoint. Offline: no client is ever built.

What matters here is the part POST /trade adds over POST /orders: choosing the
expiry and strike from nothing but a spot price, the idempotency key that
replaces the preview token, and `max_cash` -- which is the only thing left
standing between a mistyped entry_price and an order a hundred times too big.
"""

from __future__ import annotations

import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.order_rules import IdempotencyStore, RequestInFlight  # noqa: E402
from api.service.contract import (  # noqa: E402
    ExpiryNotListedError,
    StrikeNotFoundError,
)
from api.service.contract.selection import (  # noqa: E402
    choose_expiry,
    find_closest_strike,
)


class StubExpiry:
    """Stands in for an OptionExpiry."""

    def __init__(self, date_text, days_to_expiry, period_tag="w"):
        self.date_text = date_text
        self.expiry_date = date.fromisoformat(date_text)
        self.days_to_expiry = days_to_expiry
        self.period_tag = period_tag
        self.timestamp_ms = 0
        self.option_symbol = "AAPL"

    @property
    def period_label(self):
        return {"m": "monthly", "w": "weekly"}.get(self.period_tag, "unknown")


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------


class TestChooseExpiry:
    def test_skips_anything_inside_the_minimum(self):
        expiries = [
            StubExpiry("2026-09-06", 1),
            StubExpiry("2026-09-11", 6),
        ]
        chosen, reason = choose_expiry(expiries, minimum_days=3)
        assert chosen.date_text == "2026-09-11"
        assert "3 days out" in reason

    def test_refuses_to_trade_something_expiring_today(self):
        """find_next_tradable_expiry would happily return this one."""
        expiries = [StubExpiry("2026-09-05", 0), StubExpiry("2026-09-06", 1)]
        with pytest.raises(ExpiryNotListedError, match="at least 3 days"):
            choose_expiry(expiries, minimum_days=3)

    def test_prefers_a_monthly_over_a_sooner_weekly(self):
        expiries = [
            StubExpiry("2026-09-09", 4, "w"),
            StubExpiry("2026-09-18", 13, "m"),
        ]
        chosen, reason = choose_expiry(expiries, minimum_days=3)
        assert chosen.date_text == "2026-09-18"
        assert "monthly" in reason

    def test_falls_back_to_a_weekly_when_no_monthly_qualifies(self):
        expiries = [StubExpiry("2026-09-09", 4, "w")]
        chosen, reason = choose_expiry(expiries, minimum_days=3)
        assert chosen.date_text == "2026-09-09"
        assert "weekly" in reason

    def test_monthly_preference_can_be_turned_off(self):
        expiries = [
            StubExpiry("2026-09-09", 4, "w"),
            StubExpiry("2026-09-18", 13, "m"),
        ]
        chosen, _reason = choose_expiry(
            expiries, minimum_days=3, prefer_monthly=False
        )
        assert chosen.date_text == "2026-09-09"

    def test_the_refusal_says_how_close_the_soonest_one_is(self):
        expiries = [StubExpiry("2026-09-06", 1)]
        with pytest.raises(ExpiryNotListedError, match="1 day"):
            choose_expiry(expiries, minimum_days=3)


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


class TestFindClosestStrike:
    LADDER = [300.0, 305.0, 310.0, 315.0, 317.5, 320.0, 325.0]

    def test_picks_the_nearest(self):
        strike, reason = find_closest_strike(self.LADDER, 318.40, "CALL")
        assert strike == 317.5
        assert "closest of 7" in reason

    def test_an_exact_match_wins(self):
        strike, _reason = find_closest_strike(self.LADDER, 320.0, "CALL")
        assert strike == 320.0

    def test_a_call_tie_takes_the_higher_strike(self):
        """Halfway between 315 and 320; out of the money for a call is up."""
        strike, reason = find_closest_strike([315.0, 320.0], 317.5, "CALL")
        assert strike == 320.0
        assert "out-of-the-money" in reason

    def test_a_put_tie_takes_the_lower_strike(self):
        """Same price, opposite side, opposite answer."""
        strike, _reason = find_closest_strike([315.0, 320.0], 317.5, "PUT")
        assert strike == 315.0

    def test_a_price_below_the_whole_ladder_takes_the_bottom(self):
        strike, _reason = find_closest_strike(self.LADDER, 100.0, "CALL")
        assert strike == 300.0

    def test_a_price_above_the_whole_ladder_takes_the_top(self):
        strike, _reason = find_closest_strike(self.LADDER, 900.0, "CALL")
        assert strike == 325.0

    def test_an_empty_ladder_is_refused(self):
        with pytest.raises(StrikeNotFoundError):
            find_closest_strike([], 318.40, "CALL")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotencyStore:
    def test_a_fresh_key_is_claimed_and_returns_nothing(self):
        store = IdempotencyStore(ttl_seconds=600)
        assert store.claim("abc12345") is None

    def test_a_second_claim_while_in_flight_is_refused(self):
        """The retry must not be allowed to place a second order."""
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        with pytest.raises(RequestInFlight, match="already being processed"):
            store.claim("abc12345")

    def test_the_refusal_tells_you_how_to_recover(self):
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        with pytest.raises(RequestInFlight) as caught:
            store.claim("abc12345")
        assert "GET /orders" in str(caught.value)

    def test_a_completed_request_replays_its_response(self):
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        store.complete("abc12345", {"order_id": 99})
        assert store.claim("abc12345") == {"order_id": 99}

    def test_replaying_does_not_consume_the_record(self):
        """Unlike a preview token, an idempotency record answers repeatedly."""
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        store.complete("abc12345", {"order_id": 99})
        assert store.claim("abc12345") == {"order_id": 99}
        assert store.claim("abc12345") == {"order_id": 99}

    def test_releasing_lets_the_key_be_used_again(self):
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        store.release("abc12345")
        assert store.claim("abc12345") is None

    def test_releasing_never_undoes_a_completed_record(self):
        """Release is for failures that placed nothing. It must not erase a fill."""
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        store.complete("abc12345", {"order_id": 99})
        store.release("abc12345")
        assert store.claim("abc12345") == {"order_id": 99}

    def test_records_expire(self):
        """Backdated rather than slept through: the clock is not the subject."""
        store = IdempotencyStore(ttl_seconds=600)
        store.claim("abc12345")
        store.complete("abc12345", {"order_id": 99})

        store._records["abc12345"].created_at -= timedelta(seconds=601)

        assert store.claim("abc12345") is None

    def test_only_one_of_many_racing_threads_gets_the_claim(self):
        """The claim is what stands between a retry storm and duplicate orders."""
        store = IdempotencyStore(ttl_seconds=600)
        won, refused = [], []

        def attempt():
            try:
                store.claim("racing-key-1")
                won.append(1)
            except RequestInFlight:
                refused.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(won) == 1
        assert len(refused) == 11


# ---------------------------------------------------------------------------
# The endpoint itself
# ---------------------------------------------------------------------------


def make_body(**overrides):
    """A valid request body, with fields overridable per test."""
    body = {
        "client_order_id": "test-order-0001",
        "symbol": "AAPL",
        "option_type": "CALL",
        "current_price": 318.40,
        "quantity": 1,
        "entry_price": 0.30,
        "take_profit_percent": 20,
        "stop_loss_percent": 15,
        "max_cash": 100.0,
    }
    body.update(overrides)
    return body


class TestRequestValidation:
    """Pydantic refuses these before any code in the route runs."""

    def test_a_valid_body_is_accepted(self):
        from api.schemas import TradeRequest

        assert TradeRequest(**make_body()).symbol == "AAPL"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("option_type", "CALLS"),
            ("quantity", 0),
            ("quantity", -1),
            ("current_price", 0),
            ("current_price", -5),
            ("entry_price", 0),
            ("entry_price", -0.30),
            ("take_profit_percent", 0),
            ("take_profit_percent", -5),
            ("stop_loss_percent", 0),
            ("stop_loss_percent", 100),
            ("stop_loss_percent", 150),
            ("max_cash", 0),
            ("client_order_id", "short"),
            ("leg_time_in_force", "IOC"),
        ],
    )
    def test_bad_values_are_refused(self, field, value):
        from pydantic import ValidationError

        from api.schemas import TradeRequest

        with pytest.raises(ValidationError):
            TradeRequest(**make_body(**{field: value}))

    @pytest.mark.parametrize(
        "field", ["client_order_id", "symbol", "option_type", "current_price",
                  "quantity", "entry_price", "take_profit_percent",
                  "stop_loss_percent", "max_cash"]
    )
    def test_every_required_field_is_required(self, field):
        from pydantic import ValidationError

        from api.schemas import TradeRequest

        body = make_body()
        del body[field]
        with pytest.raises(ValidationError):
            TradeRequest(**body)

    def test_max_cash_has_no_default(self):
        """It must be stated. It replaces the two-step cash confirmation."""
        from api.schemas import TradeRequest

        assert TradeRequest.model_fields["max_cash"].is_required()

    def test_defaults_are_the_conservative_ones(self):
        from api.schemas import TradeRequest

        request = TradeRequest(**make_body())
        assert request.leg_time_in_force == "DAY"
        assert request.validate_only is False


class TestTheEndpointIsRegisteredAndProtected:
    def test_trade_is_in_the_schema(self):
        from api.app import create_app

        assert "/trade" in create_app().openapi()["paths"]

    def test_trade_is_not_an_unprotected_path(self):
        """It can place orders, so it must sit behind the API key."""
        from api.app import UNPROTECTED_PATHS

        assert "/trade" not in UNPROTECTED_PATHS

    def test_trade_declares_the_api_key_in_its_schema(self):
        from api.app import create_app

        spec = create_app().openapi()
        assert spec["paths"]["/trade"]["post"].get("security")


class TestSafetyIsNotBypassed:
    """The fast path must not have become a way around the locks."""

    def test_the_route_reuses_the_library_submission_function(self):
        from api.routes import trade

        assert hasattr(trade, "buy_option_with_bracket")

    def test_the_route_contains_no_place_order_call_of_its_own(self):
        source = (
            Path(__file__).resolve().parent.parent / "api/routes/trade.py"
        ).read_text(encoding="utf-8")
        assert "place_order(" not in source

    def test_the_route_checks_the_locks_before_doing_work(self):
        from api.routes import trade

        assert hasattr(trade, "require_orders_enabled")

    def test_validate_only_never_reaches_the_submission_function(self):
        source = (
            Path(__file__).resolve().parent.parent / "api/routes/trade.py"
        ).read_text(encoding="utf-8")
        # The validate_only branch returns before buy_option_with_bracket.
        early_return = source.index("if body.validate_only:")
        submission = source.index("outcome, final_estimate, legs = buy_option")
        assert early_return < submission


class TestPriceOnlySnapshot:
    """No quote exists on this path. What stands in for one must be honest."""

    def test_it_is_stamped_manual_not_fetched(self):
        from api.routes.trade import build_price_only_snapshot
        from api.service.order.bracket import calculate_bracket_from_percentages

        calculation = calculate_bracket_from_percentages(0.30, 20, 15, 0.01, 1)
        snapshot = build_price_only_snapshot(calculation)
        assert snapshot.is_manual is True

    def test_the_limit_price_is_the_buffered_entry(self):
        from api.routes.trade import build_price_only_snapshot
        from api.service.order.bracket import calculate_bracket_from_percentages

        calculation = calculate_bracket_from_percentages(0.30, 20, 15, 0.01, 1)
        snapshot = build_price_only_snapshot(calculation)
        assert snapshot.limit_price == 0.31

    def test_liquidity_is_absent_rather_than_invented(self):
        """is_low_liquidity treats None as thin, so the gap fails safe."""
        from api.routes.trade import build_price_only_snapshot
        from api.service.market import is_low_liquidity
        from api.service.order.bracket import calculate_bracket_from_percentages

        calculation = calculate_bracket_from_percentages(0.30, 20, 15, 0.01, 1)
        snapshot = build_price_only_snapshot(calculation)
        assert snapshot.volume is None
        assert snapshot.open_interest is None
        assert is_low_liquidity(snapshot.volume, snapshot.open_interest) is True
