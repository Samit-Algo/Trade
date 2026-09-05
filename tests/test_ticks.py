"""The price grid, and turning percentages into legal bracket prices.

Offline and pure. Nothing here builds a client or touches the network.

The tick size these tests use is MEASURED, not assumed -- see HANDOVER.md
section 3d. The widely quoted "penny under $3.00, nickel at $3.00 and above"
convention was tested against 32,360 real traded prices and refuted, so the
band-rule tests that would have existed here do not, on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.contract.selection import (  # noqa: E402
    DEFAULT_MIN_DAYS_TO_EXPIRY,
)
from api.service.core import config  # noqa: E402
from api.service.order import BracketError  # noqa: E402
from api.service.order.bracket import (  # noqa: E402
    calculate_bracket_from_percentages,
)
from api.service.order.ticks import (  # noqa: E402
    MEASURED_TICK_SIZE,
    TickError,
    apply_buffer,
    is_on_tick,
    snap_down,
    snap_nearest,
    snap_up,
)

PENNY = 0.01
NICKEL = 0.05


class TestTheMirroredConstants:
    """config.py copies two defaults rather than importing them.

    Importing them would make config -> contract -> broker -> config, which
    breaks the package at import time. These tests are the cheap half of what
    the import would have bought.
    """

    def test_the_tick_default_matches_the_measured_value(self):
        assert config.DEFAULT_OPTION_TICK_SIZE == MEASURED_TICK_SIZE

    def test_the_expiry_default_matches_the_selection_module(self):
        assert config.DEFAULT_MIN_DAYS_TO_EXPIRY == DEFAULT_MIN_DAYS_TO_EXPIRY


class TestIsOnTick:
    def test_a_penny_price_is_on_a_penny_grid(self):
        assert is_on_tick(8.07, PENNY) is True

    def test_a_penny_price_is_not_on_a_nickel_grid(self):
        assert is_on_tick(8.07, NICKEL) is False

    def test_a_nickel_price_is_on_both_grids(self):
        assert is_on_tick(8.05, NICKEL) is True
        assert is_on_tick(8.05, PENNY) is True

    def test_floating_point_noise_does_not_report_a_legal_price_as_illegal(self):
        """8.05 / 0.05 is 161.00000000000003 in binary floating point."""
        assert is_on_tick(8.05, NICKEL) is True
        assert is_on_tick(0.30, PENNY) is True
        assert is_on_tick(114.12, PENNY) is True


class TestSnapping:
    def test_nearest_rounds_both_ways(self):
        assert snap_nearest(8.062, PENNY) == 8.06
        assert snap_nearest(8.067, PENNY) == 8.07

    def test_nearest_leaves_a_valid_price_alone(self):
        assert snap_nearest(0.30, PENNY) == 0.30

    def test_up_never_goes_down(self):
        assert snap_up(0.372, PENNY) == 0.38
        assert snap_up(9.672, PENNY) == 9.68

    def test_up_leaves_a_price_already_on_the_grid_alone(self):
        """Ceil must not push a legal price a whole tick higher."""
        assert snap_up(0.38, PENNY) == 0.38
        assert snap_up(8.05, NICKEL) == 8.05

    def test_down_never_goes_up(self):
        assert snap_down(0.2635, PENNY) == 0.26
        assert snap_down(6.851, PENNY) == 6.85

    def test_down_leaves_a_price_already_on_the_grid_alone(self):
        assert snap_down(0.26, PENNY) == 0.26
        assert snap_down(8.05, NICKEL) == 8.05

    def test_a_coarser_grid_moves_prices_further(self):
        assert snap_up(8.06, NICKEL) == 8.10
        assert snap_down(8.06, NICKEL) == 8.05

    def test_zero_tick_is_refused_rather_than_dividing_by_zero(self):
        with pytest.raises(TickError):
            snap_nearest(1.00, 0)

    def test_negative_tick_is_refused(self):
        with pytest.raises(TickError):
            snap_up(1.00, -0.01)


class TestApplyBuffer:
    def test_one_tick_on_a_cheap_option(self):
        assert apply_buffer(0.30, PENNY, 1) == 0.31

    def test_one_tick_on_an_expensive_option(self):
        """The measured grid is pennies at every price. 8.05 -> 8.06, not 8.10."""
        assert apply_buffer(8.05, PENNY, 1) == 8.06

    def test_two_ticks(self):
        assert apply_buffer(0.30, PENNY, 2) == 0.32
        assert apply_buffer(8.05, PENNY, 2) == 8.07

    def test_zero_ticks_changes_nothing(self):
        assert apply_buffer(8.05, PENNY, 0) == 8.05

    def test_a_negative_buffer_is_refused(self):
        """A negative buffer moves a BUY away from the market."""
        with pytest.raises(TickError, match="zero or more"):
            apply_buffer(0.30, PENNY, -1)

    def test_no_floating_point_dust_survives(self):
        """31 * 0.01 is 0.31000000000000005 before cleaning."""
        assert apply_buffer(0.30, PENNY, 1) == 0.31
        assert repr(apply_buffer(0.30, PENNY, 1)) == "0.31"


class TestBracketFromPercentages:
    def test_the_worked_example_from_the_plan(self):
        result = calculate_bracket_from_percentages(0.30, 20, 15, PENNY, 1)
        assert result.entry_snapped == 0.30
        assert result.entry_actual == 0.31
        assert result.take_profit_raw == pytest.approx(0.372)
        assert result.take_profit_price == 0.38
        assert result.stop_loss_raw == pytest.approx(0.2635)
        assert result.stop_loss_price == 0.26

    def test_an_expensive_contract_on_the_measured_penny_grid(self):
        result = calculate_bracket_from_percentages(8.05, 20, 15, PENNY, 1)
        assert result.entry_actual == 8.06
        assert result.take_profit_price == 9.68
        assert result.stop_loss_price == 6.85

    def test_percentages_apply_to_the_buffered_price_not_the_requested_one(self):
        """20% of 0.31 is 0.372, not 20% of 0.30 which is 0.360."""
        result = calculate_bracket_from_percentages(0.30, 20, 15, PENNY, 1)
        assert result.take_profit_raw == pytest.approx(0.31 * 1.20)
        assert result.take_profit_raw != pytest.approx(0.30 * 1.20)

    def test_take_profit_never_rounds_down(self):
        for entry in (0.30, 0.55, 1.23, 8.05, 44.44):
            result = calculate_bracket_from_percentages(entry, 20, 15, PENNY, 1)
            assert result.take_profit_price >= result.take_profit_raw

    def test_stop_loss_never_rounds_up(self):
        for entry in (0.30, 0.55, 1.23, 8.05, 44.44):
            result = calculate_bracket_from_percentages(entry, 20, 15, PENNY, 1)
            assert result.stop_loss_price <= result.stop_loss_raw

    def test_rounding_only_ever_widens_the_bracket(self):
        for entry in (0.30, 0.55, 1.23, 8.05, 44.44):
            result = calculate_bracket_from_percentages(entry, 20, 15, PENNY, 1)
            assert result.stop_loss_price < result.entry_actual
            assert result.take_profit_price > result.entry_actual

    def test_both_final_prices_sit_on_the_grid(self):
        for entry in (0.07, 0.30, 1.23, 8.05, 114.12):
            result = calculate_bracket_from_percentages(entry, 23, 17, PENNY, 1)
            assert is_on_tick(result.entry_actual, PENNY)
            assert is_on_tick(result.take_profit_price, PENNY)
            assert is_on_tick(result.stop_loss_price, PENNY)

    def test_a_stop_that_rounds_down_to_nothing_is_refused(self):
        """A 15% stop on a one-cent option floors straight to zero."""
        with pytest.raises(BracketError, match="minimum increment"):
            calculate_bracket_from_percentages(0.01, 20, 15, PENNY, 0)

    def test_the_refusal_names_a_way_out(self):
        with pytest.raises(BracketError) as caught:
            calculate_bracket_from_percentages(0.01, 20, 15, PENNY, 0)
        assert "smaller stop percentage" in str(caught.value)

    def test_a_coarse_grid_produces_coarse_prices(self):
        """Proves the tick is a parameter, not a constant baked into the maths."""
        result = calculate_bracket_from_percentages(8.05, 20, 15, NICKEL, 1)
        assert result.entry_actual == 8.10
        assert result.take_profit_price == 9.75
        assert result.stop_loss_price == 6.85

    def test_the_working_is_kept_for_the_response(self):
        result = calculate_bracket_from_percentages(0.30, 20, 15, PENNY, 1)
        assert result.entry_requested == 0.30
        assert result.tick_size == PENNY
        assert result.buffer_ticks == 1
        assert "widens" in result.rounding_note
