"""Unit tests for Phase 6 position valuation. Offline, no network."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tiger_backend.positions import (  # noqa: E402
    DEFAULT_EXPIRY_WARNING_DAYS,
    OptionPosition,
    build_expiry_warning,
    build_option_position,
    calculate_assignment_exposure,
    calculate_cost_basis,
    calculate_current_value,
    calculate_pnl_percent,
    calculate_unrealised_pnl,
    is_expiring_soon,
    read_position_quantity,
    value_position,
)
from tiger_backend.providers import BidSnapshot, QuoteSource  # noqa: E402


class StubContract:
    """A Contract as get_positions really returns it: strike is a STRING."""

    def __init__(self, strike="360.0", expiry="20260918", put_call="CALL"):
        self.identifier = "AAPL  260918C00360000"
        self.symbol = "AAPL"
        self.sec_type = "OPT"
        self.expiry = expiry
        self.strike = strike
        self.put_call = put_call
        self.multiplier = 100.0
        self.name = "Apple"


class StubPosition:
    """A Position as get_positions really returns it."""

    def __init__(self, quantity=1, position_qty=1.0, average_cost=0.3102):
        self.contract = StubContract()
        self.quantity = quantity
        self.position_scale = 0
        self.position_qty = position_qty
        self.average_cost = average_cost
        self.market_price = 0.28
        self.market_value = 28.0
        self.unrealized_pnl = -3.02


def make_position(days_to_expiry=15, strike=360.0, quantity=1.0, put_call="CALL"):
    return OptionPosition(
        identifier="AAPL  260918C00360000",
        underlying="AAPL",
        expiry_date_text="2026-09-18",
        expiry_compact="20260918",
        strike=strike,
        put_call=put_call,
        multiplier=100.0,
        quantity=quantity,
        average_cost=0.3102,
        days_to_expiry=days_to_expiry,
        market_price_latest=0.28,
        tiger_unrealised_pnl=-3.02,
    )


class TestPnLArithmetic:
    def test_the_spec_formula(self):
        """(current_bid - avg_entry) x multiplier x quantity."""
        assert calculate_unrealised_pnl(0.27, 0.3102, 100.0, 1.0) == -4.02

    def test_a_profit_is_positive(self):
        assert calculate_unrealised_pnl(0.50, 0.3102, 100.0, 1.0) == 18.98

    def test_quantity_scales_the_result(self):
        one = calculate_unrealised_pnl(0.27, 0.3102, 100.0, 1.0)
        three = calculate_unrealised_pnl(0.27, 0.3102, 100.0, 3.0)
        assert three == pytest.approx(one * 3)

    def test_cost_basis_uses_the_commission_inclusive_average(self):
        """A $28 fill cost $31.02 all in. The real number is the useful one."""
        assert calculate_cost_basis(0.3102, 100.0, 1.0) == 31.02

    def test_current_value_is_at_the_bid(self):
        assert calculate_current_value(0.27, 100.0, 1.0) == 27.00

    def test_pnl_percent(self):
        assert calculate_pnl_percent(-4.02, 31.02) == -13.0

    def test_pnl_percent_of_nothing_is_none(self):
        assert calculate_pnl_percent(-4.02, 0) is None


class TestAssignmentExposure:
    def test_the_number_the_warning_exists_for(self):
        """One AAPL 360 call costing $31 exercises into $36,000 of stock."""
        assert calculate_assignment_exposure(360.0, 100.0, 1.0) == 36_000.00

    def test_it_scales_with_quantity(self):
        assert calculate_assignment_exposure(360.0, 100.0, 3.0) == 108_000.00

    def test_it_has_nothing_to_do_with_what_the_option_cost(self):
        cost = calculate_cost_basis(0.3102, 100.0, 1.0)
        exposure = calculate_assignment_exposure(360.0, 100.0, 1.0)
        assert exposure > cost * 1000


class TestExpiryWarning:
    def test_the_default_threshold_is_three_days(self):
        assert DEFAULT_EXPIRY_WARNING_DAYS == 3

    def test_warns_at_or_below_the_threshold(self):
        assert is_expiring_soon(3, 3) is True
        assert is_expiring_soon(2, 3) is True
        assert is_expiring_soon(0, 3) is True

    def test_does_not_warn_above_the_threshold(self):
        assert is_expiring_soon(4, 3) is False
        assert is_expiring_soon(15, 3) is False

    def test_the_threshold_is_configurable(self):
        assert is_expiring_soon(15, 20) is True

    def test_the_warning_names_the_cash_an_exercise_would_need(self):
        lines = build_expiry_warning(make_position(days_to_expiry=2), 3)
        text = "\n".join(lines)
        assert "$36,000.00" in text

    def test_the_warning_explains_auto_exercise_not_just_time_decay(self):
        lines = build_expiry_warning(make_position(days_to_expiry=2), 3)
        text = "\n".join(lines).lower()
        assert "exercised automatically" in text

    def test_a_call_warns_about_buying_above_the_strike(self):
        text = "\n".join(build_expiry_warning(make_position(2, put_call="CALL"), 3))
        assert "buy" in text and "above" in text

    def test_a_put_warns_about_selling_below_the_strike(self):
        text = "\n".join(build_expiry_warning(make_position(2, put_call="PUT"), 3))
        assert "sell" in text and "below" in text

    def test_expires_today_is_worded_differently(self):
        text = "\n".join(build_expiry_warning(make_position(0), 3))
        assert "EXPIRES TODAY" in text

    def test_already_expired_is_worded_differently(self):
        text = "\n".join(build_expiry_warning(make_position(-2), 3))
        assert "EXPIRED 2 day(s) ago" in text

    def test_no_warning_when_far_from_expiry(self):
        assert build_expiry_warning(make_position(30), 3) == []


class TestReadingTigersFields:
    def test_position_qty_is_preferred_over_legacy_quantity(self):
        raw = StubPosition(quantity=999, position_qty=2.0)
        assert read_position_quantity(raw) == 2.0

    def test_quantity_is_the_fallback(self):
        raw = StubPosition(quantity=5)
        del raw.position_qty
        assert read_position_quantity(raw) == 5.0

    def test_string_strike_is_converted_to_a_float(self):
        """strike arrives as '360.0', the same quirk the strike ladder has."""
        position = build_option_position(StubPosition())
        assert position.strike == 360.0
        assert isinstance(position.strike, float)

    def test_compact_expiry_is_converted_for_display(self):
        position = build_option_position(StubPosition())
        assert position.expiry_compact == "20260918"
        assert position.expiry_date_text == "2026-09-18"

    def test_tigers_own_figures_are_kept_but_separate(self):
        """market_price is latestPrice. It is recorded, never used to value."""
        position = build_option_position(StubPosition())
        assert position.market_price_latest == 0.28
        assert position.tiger_unrealised_pnl == -3.02

    def test_a_position_without_a_contract_is_skipped(self):
        raw = StubPosition()
        raw.contract = None
        assert build_option_position(raw) is None


class TestValuePosition:
    def make_bid(self, bid=0.27):
        return BidSnapshot(
            bid=bid,
            source=QuoteSource.MANUAL,
            captured_at=datetime.now(timezone.utc),
        )

    def test_values_at_the_bid_not_at_tigers_market_price(self):
        position = make_position()
        valuation = value_position(position, self.make_bid(0.27), 60)

        assert valuation.current_value == 27.00
        assert valuation.unrealised_pnl == -4.02
        # Tiger says -3.02 from latestPrice 0.28. Ours differs, deliberately.
        assert valuation.unrealised_pnl != position.tiger_unrealised_pnl

    def test_the_bid_carries_its_manual_label(self):
        valuation = value_position(make_position(), self.make_bid(), 60)
        assert valuation.bid_source_tag == "[MANUAL]"

    def test_a_fresh_bid_is_not_stale(self):
        valuation = value_position(make_position(), self.make_bid(), 60)
        assert valuation.bid_is_stale is False

    def test_profitability_flag(self):
        assert value_position(make_position(), self.make_bid(0.50), 60).is_profitable
        assert not value_position(make_position(), self.make_bid(0.10), 60).is_profitable
