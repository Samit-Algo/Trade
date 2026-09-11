"""Unit tests for Phase 4 cost arithmetic. Offline, no network, no SDK calls."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.contract import OptionContractInfo  # noqa: E402
from api.service.order import (  # noqa: E402
    PricingError,
    calculate_break_even,
    calculate_maximum_loss,
    calculate_total_cash,
    choose_price,
    compare_to_available_cash,
    estimate_cost,
    normalise_limit_price,
    validate_action,
    validate_quantity,
)


def make_contract(put_call: str = "CALL", strike: float = 320.0, multiplier: float = 100.0):
    """Build an OptionContractInfo for tests."""
    return OptionContractInfo(
        identifier="AAPL  260918C00320000",
        underlying="AAPL",
        expiry_date_text="2026-09-18",
        expiry_compact="20260918",
        strike=strike,
        put_call=put_call,
        multiplier=multiplier,
        contract_id=353122977,
        days_to_expiry=15,
        name="Apple",
    )


class TestValidateAction:
    def test_accepts_buy_and_sell(self):
        assert validate_action("buy") == "BUY"
        assert validate_action(" SELL ") == "SELL"

    def test_rejects_anything_else(self):
        with pytest.raises(PricingError, match="BUY or SELL"):
            validate_action("HOLD")


class TestValidateQuantity:
    def test_accepts_positive_whole_numbers(self):
        assert validate_quantity(1) == 1
        assert validate_quantity(10) == 10

    def test_rejects_zero_and_negative(self):
        with pytest.raises(PricingError):
            validate_quantity(0)
        with pytest.raises(PricingError):
            validate_quantity(-1)

    def test_rejects_a_bool(self):
        """True is an int in Python. One contract is not the same as True."""
        with pytest.raises(PricingError):
            validate_quantity(True)


class TestChoosePrice:
    def test_buy_uses_the_ask(self):
        price, reason = choose_price("BUY", bid=5.00, ask=5.20)
        assert price == 5.20
        assert "ask" in reason

    def test_sell_uses_the_bid(self):
        price, reason = choose_price("SELL", bid=5.00, ask=5.20)
        assert price == 5.00
        assert "bid" in reason

    def test_never_uses_the_midpoint(self):
        """The midpoint is not a price anyone will trade with you at."""
        buy_price, _ = choose_price("BUY", bid=5.00, ask=5.20)
        sell_price, _ = choose_price("SELL", bid=5.00, ask=5.20)
        assert 5.10 not in (buy_price, sell_price)


class TestCalculateTotalCash:
    def test_one_contract_is_a_hundred_shares(self):
        """The whole point: quantity=1 is $520, not $5.20."""
        assert calculate_total_cash(price=5.20, multiplier=100, quantity=1) == 520.0

    def test_quantity_multiplies(self):
        assert calculate_total_cash(price=5.20, multiplier=100, quantity=3) == 1560.0

    def test_a_mistyped_quantity_is_visible(self):
        one = calculate_total_cash(5.20, 100, 1)
        ten = calculate_total_cash(5.20, 100, 10)
        assert ten == one * 10


class TestBreakEven:
    def test_call_break_even_is_strike_plus_premium(self):
        assert calculate_break_even("CALL", strike=320.0, premium_per_share=5.20) == 325.20

    def test_put_break_even_is_strike_minus_premium(self):
        assert calculate_break_even("PUT", strike=300.0, premium_per_share=5.20) == 294.80

    def test_unknown_type_gives_none(self):
        assert calculate_break_even("STRADDLE", 320.0, 5.20) is None


class TestMaximumLoss:
    def test_buying_risks_the_premium_and_no_more(self):
        loss, note = calculate_maximum_loss("BUY", "CALL", total_cash=520.0)
        assert loss == 520.0
        assert "100%" in note

    def test_selling_a_call_is_not_given_a_comforting_number(self):
        """A short call has no bounded loss. Printing one would be a lie."""
        loss, note = calculate_maximum_loss("SELL", "CALL", total_cash=520.0)
        assert loss is None
        assert "UNBOUNDED" in note

    def test_selling_a_put_is_also_unbounded_by_this_function(self):
        loss, note = calculate_maximum_loss("SELL", "PUT", total_cash=520.0)
        assert loss is None
        assert "strike" in note


class TestEstimateCost:
    def test_buy_uses_the_limit_price_for_cash(self):
        estimate = estimate_cost(
            make_contract(), action="BUY", quantity=1, bid=5.00, ask=5.20, limit_price=5.20
        )
        assert estimate.total_cash == 520.0
        assert estimate.shares_of_exposure == 100
        assert estimate.cash_label == "CASH REQUIRED"

    def test_sell_is_cash_received_not_required(self):
        estimate = estimate_cost(
            make_contract("PUT", 300.0), action="SELL", quantity=2,
            bid=0.55, ask=0.60, limit_price=0.55,
        )
        assert estimate.total_cash == 110.0
        assert estimate.cash_label == "CASH RECEIVED"

    def test_without_a_limit_price_a_buy_falls_back_to_the_ask(self):
        estimate = estimate_cost(
            make_contract(), action="BUY", quantity=1, bid=5.00, ask=5.20
        )
        assert estimate.price_used == 5.20

    def test_zero_price_is_refused(self):
        with pytest.raises(PricingError, match="cannot be traded"):
            estimate_cost(
                make_contract(), action="BUY", quantity=1, bid=0.0, ask=0.0
            )

    def test_break_even_appears_on_the_estimate(self):
        estimate = estimate_cost(
            make_contract(), action="BUY", quantity=1, bid=5.00, ask=5.20, limit_price=5.20
        )
        assert estimate.break_even_price == 325.20


class TestNormaliseLimitPrice:
    def test_snaps_when_a_tick_is_known(self):
        price, was_snapped = normalise_limit_price(5.23, min_tick=0.05)
        assert price == pytest.approx(5.25)
        assert was_snapped is True

    def test_passes_through_untouched_when_the_tick_is_unknown(self):
        """Tiger returns min_tick=None, and a guessed tick would be worse."""
        price, was_snapped = normalise_limit_price(5.23, min_tick=None)
        assert price == 5.23
        assert was_snapped is False

    def test_zero_tick_is_treated_as_unknown(self):
        price, was_snapped = normalise_limit_price(5.23, min_tick=0)
        assert price == 5.23
        assert was_snapped is False


class TestCompareToAvailableCash:
    def test_affordable_gives_no_warning(self):
        assert compare_to_available_cash(520.0, 1_000_000.0) is None

    def test_unaffordable_warns_and_names_the_shortfall(self):
        warning = compare_to_available_cash(2000.0, 500.0)
        assert "1,500.00" in warning

    def test_warning_mentions_that_buying_power_is_borrowed(self):
        warning = compare_to_available_cash(2000.0, 500.0)
        assert "borrowed" in warning

    def test_unknown_cash_says_so_rather_than_passing_silently(self):
        assert compare_to_available_cash(520.0, None) is not None
