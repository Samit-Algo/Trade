"""Unit tests for Phase 7 bracket logic. Offline -- nothing is submitted."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.order import (  # noqa: E402
    COMMISSION_BASE,
    COMMISSION_PER_CONTRACT,
    BracketError,
    BracketLegs,
    build_option_order_with_bracket,
    calculate_intended_risk,
    estimate_commission_per_order,
    estimate_commission_per_share,
    estimate_round_trip_commission,
    is_take_profit_a_losing_exit,
    validate_bracket_prices,
)
from tests.test_orders import make_contract  # noqa: E402


class StubSettings:
    account = "20191106192858300"


class TestAttachType:
    def test_two_legs_send_brackets(self):
        """The SDK encodes both legs as attach_type BRACKETS."""
        assert BracketLegs(0.60, 0.15, "DAY").attach_type == "BRACKETS"


class TestCommissionEstimates:
    """The model is fitted to four real orders; these lock in all four."""

    def test_reproduces_the_one_contract_observation(self):
        assert estimate_commission_per_order(1) == 3.02

    def test_reproduces_the_three_contract_observation(self):
        assert estimate_commission_per_order(3) == 3.09

    def test_is_not_flat(self):
        """Flat would have predicted 3.02 for three contracts."""
        assert estimate_commission_per_order(3) != estimate_commission_per_order(1)

    def test_is_not_per_contract(self):
        """Per contract would have predicted 9.06 for three."""
        assert estimate_commission_per_order(3) < estimate_commission_per_order(1) * 3

    def test_the_base_dominates(self):
        """Tripling the size adds seven cents, not triple the fee."""
        difference = estimate_commission_per_order(3) - estimate_commission_per_order(1)
        assert difference == pytest.approx(0.07, abs=0.005)

    def test_round_trip_is_two_orders(self):
        assert estimate_round_trip_commission(1) == 6.04
        assert estimate_round_trip_commission(3) == 6.18

    def test_round_trip_takes_no_multiplier(self):
        """Commission is per contract, not per share. The distinction matters."""
        import inspect

        parameters = inspect.signature(estimate_round_trip_commission).parameters
        assert "multiplier" not in parameters

    def test_per_share_spreads_across_the_contract(self):
        assert estimate_commission_per_share(1, 100) == pytest.approx(0.0302)

    def test_more_contracts_dilute_the_per_share_cost(self):
        assert estimate_commission_per_share(10, 100) < estimate_commission_per_share(1, 100)

    def test_model_constants_are_the_fitted_values(self):
        assert COMMISSION_BASE == 2.985
        assert COMMISSION_PER_CONTRACT == 0.035


class TestLosingExitWarning:
    def test_a_target_below_entry_plus_commission_loses(self):
        assert is_take_profit_a_losing_exit(0.31, 0.30, 1, 100) is True

    def test_a_target_exactly_at_break_even_still_loses(self):
        break_even = 0.30 + estimate_commission_per_share(1, 100)
        assert is_take_profit_a_losing_exit(break_even, 0.30, 1, 100) is True

    def test_a_real_profit_target_does_not_warn(self):
        assert is_take_profit_a_losing_exit(0.60, 0.30, 1, 100) is False

    def test_the_warning_scales_with_quantity(self):
        """Ten contracts spread the same fee thinner, so the bar is lower."""
        assert is_take_profit_a_losing_exit(0.305, 0.30, 1, 100) is True
        assert is_take_profit_a_losing_exit(0.305, 0.30, 10, 100) is False


class TestValidateBracketPrices:
    def test_a_sane_bracket_passes(self):
        validate_bracket_prices(0.30, 0.60, 0.15)

    def test_stop_at_or_above_entry_is_refused(self):
        with pytest.raises(BracketError, match="trigger immediately"):
            validate_bracket_prices(0.30, 0.60, 0.30)

    def test_target_at_or_below_entry_is_refused(self):
        with pytest.raises(BracketError, match="not a profit target"):
            validate_bracket_prices(0.30, 0.30, 0.15)

    def test_an_inverted_bracket_is_caught_by_the_entry_checks(self):
        """No separate inverted check exists, and none is needed.

        stop < entry and take > entry together force take > stop, so an
        inverted bracket always trips one of the two earlier guards first.
        """
        with pytest.raises(BracketError, match="not a profit target"):
            validate_bracket_prices(0.30, 0.20, 0.25)

    def test_ordering_is_guaranteed_once_validation_passes(self):
        entry, take, stop = 0.30, 0.60, 0.15
        validate_bracket_prices(entry, take, stop)
        assert stop < entry < take

    def test_zero_prices_are_refused(self):
        with pytest.raises(BracketError, match="greater than zero"):
            validate_bracket_prices(0.30, 0.60, 0.0)


class TestIntendedRisk:
    def test_includes_round_trip_commission(self):
        """(0.30 - 0.15) x 100 x 1 = 15.00, plus 6.04 of commission."""
        assert calculate_intended_risk(0.30, 0.15, 1, 100) == 21.04


class TestBuiltBracketOrder:
    def test_both_legs_are_attached(self):
        order = build_option_order_with_bracket(
            StubSettings(), make_contract(), "BUY", 1, 0.30,
            take_profit_price=0.60, stop_loss_price=0.15,
        )
        leg_types = [leg.leg_type for leg in order.order_legs]
        assert sorted(leg_types) == ["LOSS", "PROFIT"]

    def test_leg_prices_are_carried_through(self):
        order = build_option_order_with_bracket(
            StubSettings(), make_contract(), "BUY", 1, 0.30,
            take_profit_price=0.60, stop_loss_price=0.15,
        )
        by_type = {leg.leg_type: leg.price for leg in order.order_legs}
        assert by_type["PROFIT"] == 0.60
        assert by_type["LOSS"] == 0.15

    def test_legs_default_to_day_not_gtc(self):
        """Paper rejects GTC on the parent; DAY is the SDK's own leg default."""
        order = build_option_order_with_bracket(
            StubSettings(), make_contract(), "BUY", 1, 0.30,
            take_profit_price=0.60, stop_loss_price=0.15,
        )
        assert all(leg.time_in_force == "DAY" for leg in order.order_legs)

    def test_leg_time_in_force_is_parameterised(self):
        order = build_option_order_with_bracket(
            StubSettings(), make_contract(), "BUY", 1, 0.30,
            take_profit_price=0.60, stop_loss_price=0.15,
            leg_time_in_force="GTC",
        )
        assert all(leg.time_in_force == "GTC" for leg in order.order_legs)

    def test_extended_hours_stay_off(self):
        order = build_option_order_with_bracket(
            StubSettings(), make_contract(), "BUY", 1, 0.30,
            take_profit_price=0.60, stop_loss_price=0.15,
        )
        assert order.outside_rth is False
        assert all(leg.outside_rth is False for leg in order.order_legs)
