"""The single-call trading endpoint. Offline: no client is ever built.

What matters here is the part POST /trade adds over POST /orders: choosing the
expiry and strike from nothing but a spot price, and the idempotency key that
replaces the preview token.
"""

from __future__ import annotations

import inspect
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


def make_contract():
    """A verified contract, as find_option_contract would return one."""
    from api.service.contract import OptionContractInfo

    return OptionContractInfo(
        identifier="TSLA  260909C00357500",
        underlying="TSLA",
        name="Tesla",
        expiry_date_text="2026-09-09",
        expiry_compact="20260909",
        strike=357.5,
        put_call="CALL",
        multiplier=100.0,
        contract_id=1,
        days_to_expiry=1,
        min_tick=None,
    )


def TradeRequestFactory(**overrides):
    """A valid TradeRequest for response-building tests."""
    from api.schemas import TradeRequest

    return TradeRequest(**{**make_body(), **overrides})


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
                  "quantity", "take_profit_percent", "stop_loss_percent"]
    )
    def test_every_required_field_is_required(self, field):
        from pydantic import ValidationError

        from api.schemas import TradeRequest

        body = make_body()
        del body[field]
        with pytest.raises(ValidationError):
            TradeRequest(**body)

    def test_entry_price_and_expiry_are_optional(self):
        """Absent means: fetch the price, and auto-pick the expiry."""
        from api.schemas import TradeRequest

        body = make_body()
        del body["entry_price"]
        request = TradeRequest(**body)
        assert request.entry_price is None
        assert request.expiry is None

    def test_an_explicit_expiry_is_kept(self):
        from api.schemas import TradeRequest

        request = TradeRequest(**make_body(expiry="2026-09-18"))
        assert request.expiry == "2026-09-18"

    def test_defaults_are_the_conservative_ones(self):
        from api.schemas import TradeRequest

        request = TradeRequest(**make_body())
        assert request.leg_time_in_force == "DAY"
        assert request.validate_only is False


class TestTheEndpointIsRegisteredAndProtected:
    def test_trade_is_in_the_schema(self):
        from api.main import create_app

        assert "/trade" in create_app().openapi()["paths"]

    def test_trade_is_not_an_unprotected_path(self):
        """It can place orders, so it must sit behind the API key."""
        from api.main import UNPROTECTED_PATHS

        assert "/trade" not in UNPROTECTED_PATHS

    def test_trade_declares_the_api_key_in_its_schema(self):
        from api.main import create_app

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

        assert hasattr(trade, "check_safety_locks")

    def test_only_one_function_can_submit(self):
        """buy_option_with_bracket is called from submit_and_record, nowhere else."""
        from api.routes import trade

        source = (
            Path(__file__).resolve().parent.parent / "api/routes/trade.py"
        ).read_text(encoding="utf-8")
        calls = source.count("buy_option_with_bracket(")
        assert calls == 1, f"expected one call site, found {calls}"

        submit_starts = source.index("def submit_and_record(")
        assert source.rindex("buy_option_with_bracket(") > submit_starts

    def test_prepare_trade_cannot_send_an_order(self):
        """The whole safety of releasing an idempotency key rests on this."""
        import inspect

        from api.routes import trade

        source = inspect.getsource(trade.prepare_trade)
        assert "buy_option_with_bracket" not in source
        assert "place_order" not in source

    def test_validate_only_returns_before_submitting(self):
        import inspect

        from api.routes import trade

        source = inspect.getsource(trade.place_bracketed_trade)
        assert source.index("if body.validate_only:") < source.index(
            "submit_and_record("
        )


class TestPriceOnlyQuote:
    """No quote exists on this path. What stands in for one must be honest."""

    def test_it_is_stamped_manual_not_fetched(self):
        from api.order_rules import build_price_only_quote

        assert build_price_only_quote(0.31).is_manual is True

    def test_the_one_price_becomes_bid_ask_and_limit(self):
        from api.order_rules import build_price_only_quote

        quote = build_price_only_quote(0.31)
        assert quote.limit_price == 0.31
        assert quote.bid == 0.31
        assert quote.ask == 0.31

    def test_liquidity_is_absent_rather_than_invented(self):
        """is_low_liquidity treats None as thin, so the gap fails safe."""
        from api.order_rules import build_price_only_quote
        from api.service.market import is_low_liquidity

        quote = build_price_only_quote(0.31)
        assert quote.volume is None
        assert quote.open_interest is None
        assert is_low_liquidity(quote.volume, quote.open_interest) is True


class TestTheResponseMatchesFillOutcome:
    """A real order filled and the response builder crashed on outcome.filled.

    The field is filled_quantity. Nothing caught it because no test ever built
    a response from a real FillOutcome -- the earlier tests only read the
    source text. These do the real thing.
    """

    def make_outcome(self):
        from api.service.order import FillOutcome

        return FillOutcome(
            order_id=44561393351150592,
            status="FILLED",
            requested_quantity=1,
            filled_quantity=1,
            average_fill_price=5.04,
            actual_cash=504.0,
            outcome="FULLY FILLED",
            poll_attempts=1,
            reached_terminal_status=True,
            reason="",
        )

    def test_every_attribute_the_route_reads_exists(self):
        """Catches a renamed or mistyped field without placing an order."""
        import re

        from api.routes import trade

        source = inspect.getsource(trade.submit_and_record)
        outcome = self.make_outcome()
        for attribute in set(re.findall(r"\boutcome\.(\w+)", source)):
            assert hasattr(outcome, attribute), (
                f"submit_and_record reads outcome.{attribute}, "
                f"which FillOutcome does not have"
            )

    def test_a_filled_response_can_actually_be_built(self):
        from api.routes.trade import TradePlan, build_response
        from api.order_rules import build_price_only_quote
        from api.schemas import PriceSource, shape_submitted_legs
        from api.service.order import calculate_bracket_from_percentages, estimate_cost

        contract = make_contract()
        calculation = calculate_bracket_from_percentages(5.04, 5, 2, 0.01, 0)
        quote = build_price_only_quote(calculation.entry_actual)
        plan = TradePlan(
            contract=contract,
            expiry_reason="test",
            strike_reason="test",
            calculation=calculation,
            quote=quote,
            estimate=estimate_cost(
                contract=contract, action="BUY", quantity=1,
                bid=quote.bid, ask=quote.ask, limit_price=quote.limit_price,
            ),
            price_source=PriceSource(
                source="caller", price=5.04, age_seconds=None, note="test"
            ),
        )
        outcome = self.make_outcome()

        response = build_response(
            plan,
            TradeRequestFactory(),
            order_id=outcome.order_id,
            order_status=outcome.status,
            parent_filled=outcome.filled_quantity,
            legs_submitted=shape_submitted_legs(calculation, "DAY"),
            audit_log="order_audit.log",
        )

        assert response.order_id == 44561393351150592
        assert response.parent_filled == 1
        assert len(response.legs_submitted) == 2


class TestWorkingOrderRoles:
    """Tiger reports a side and an order type, never a purpose.

    A position that looks fine in a holdings list can have no stop at all --
    that happened on 2026-09-08 when DAY legs expired overnight. Reading the
    purpose off the side and type is what makes that visible.
    """

    def test_a_sell_stop_is_protection(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("SELL", "STP") == "STOP_LOSS"

    def test_a_sell_limit_is_a_target(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("SELL", "LMT") == "TAKE_PROFIT"

    def test_a_buy_is_an_entry_that_has_not_filled(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("BUY", "LMT") == "ENTRY"

    def test_a_stop_limit_still_counts_as_a_stop(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("SELL", "STP_LMT") == "STOP_LOSS"

    def test_it_is_case_insensitive(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("sell", "stp") == "STOP_LOSS"

    def test_anything_unrecognised_is_other_not_a_guess(self):
        from api.routes.positions import classify_working_order

        assert classify_working_order("SELL", "TRAIL") == "OTHER"
        assert classify_working_order("", "") == "OTHER"


class TestOtmWholeStrikeSelection:
    """Whole strikes only, never in the money, Nth one out.

    Measured on 2026-09-09: whole strikes carried 3.5x the volume of half
    strikes, and out-of-the-money contracts moved nearly twice the percentage
    per minute of in-the-money ones.
    """

    LADDER = [365.0, 367.5, 370.0, 372.5, 375.0, 377.5, 380.0, 382.5, 385.0]
    SPOT = 371.79

    def pick(self, side, out=1, ladder=None, spot=None):
        from api.service.contract import find_otm_whole_strike

        return find_otm_whole_strike(
            ladder if ladder is not None else self.LADDER,
            self.SPOT if spot is None else spot,
            side,
            out,
        )

    def test_a_call_takes_the_first_whole_strike_above_spot(self):
        strike, _ = self.pick("CALL", 1)
        assert strike == 375.0          # not 372.5, which is a half strike

    def test_a_put_takes_the_first_whole_strike_below_spot(self):
        strike, _ = self.pick("PUT", 1)
        assert strike == 370.0

    def test_the_second_one_out(self):
        assert self.pick("CALL", 2)[0] == 380.0
        assert self.pick("PUT", 2)[0] == 365.0

    def test_half_strikes_are_never_chosen(self):
        for side in ("CALL", "PUT"):
            for out in (1, 2, 3):
                strike, _ = self.pick(side, out)
                assert strike == int(strike), f"{side} {out} picked {strike}"

    def test_an_in_the_money_strike_is_never_chosen(self):
        assert self.pick("CALL", 1)[0] > self.SPOT
        assert self.pick("PUT", 1)[0] < self.SPOT

    def test_a_strike_exactly_on_the_spot_is_at_the_money_not_out(self):
        strike, _ = self.pick("CALL", 1, ladder=[370.0, 375.0], spot=370.0)
        assert strike == 375.0

    def test_asking_further_out_than_exists_takes_the_furthest(self):
        strike, reason = self.pick("CALL", 9)
        assert strike == 385.0
        assert "only" in reason

    def test_it_falls_back_when_no_whole_strike_is_out_of_the_money(self):
        """A thinner strike beats no trade, but the reason has to say so."""
        strike, reason = self.pick("CALL", 1, ladder=[370.0, 372.5], spot=371.0)
        assert strike == 372.5
        assert "NO WHOLE" in reason

    def test_nothing_out_of_the_money_is_refused(self):
        from api.service.contract import StrikeNotFoundError

        with pytest.raises(StrikeNotFoundError, match="out of the money"):
            self.pick("CALL", 1, ladder=[300.0, 305.0], spot=400.0)

    def test_the_reason_always_names_the_rule(self):
        _strike, reason = self.pick("CALL", 1)
        assert "whole strike 1 out of the money" in reason


class TestWholeStrikeTest:
    """`% 5` would wipe out every strike on a cheap stock."""

    def test_round_numbers_are_whole(self):
        from api.service.contract import is_whole_strike

        assert is_whole_strike(375.0) is True
        assert is_whole_strike(41.0) is True

    def test_half_strikes_are_not(self):
        from api.service.contract import is_whole_strike

        assert is_whole_strike(377.5) is False
        assert is_whole_strike(40.5) is False

    def test_a_dollar_ladder_keeps_every_strike(self):
        """On a $30 stock the strikes are 29, 30, 31 -- filtering none."""
        from api.service.contract import is_whole_strike

        ladder = [29.0, 30.0, 31.0, 32.0]
        assert [k for k in ladder if is_whole_strike(k)] == ladder


class TestLiveTradingTest:
    """age_seconds counts from the bar's MINUTE START, not the last trade.

    Measured live: a bar labelled 10:06 read at 10:06:58 reported an age of
    58s while its close moved every two seconds and its volume went 518 ->
    637. So age cannot answer "is this fresh"; trading-this-minute can.
    """

    def make(self, minutes_ago, volume):
        from datetime import datetime, timezone

        from api.service.market import RecentTrade

        now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
        bar_ms = (int(now_ms // 60000) - minutes_ago) * 60000
        return RecentTrade(
            price=5.00,
            bar_time_ms=bar_ms,
            age_seconds=(now_ms - bar_ms) / 1000.0,
            volume=volume,
        )

    def test_trading_this_minute_is_live(self):
        assert self.make(0, 500).is_live is True

    def test_a_previous_minute_is_not_live(self):
        assert self.make(1, 500).is_live is False

    def test_this_minute_with_no_volume_is_not_live(self):
        """A placeholder bar carries an older trade forward."""
        assert self.make(0, 0).is_live is False

    def test_a_high_age_can_still_be_live(self):
        """The whole point: 58 seconds into the current minute is fine."""
        live = self.make(0, 500)
        assert live.age_seconds >= 0
        assert live.is_live is True
