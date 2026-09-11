"""The optional premium-scaled buy buffer.

The whole feature must be switchable off, so the off case is tested as
carefully as the on case: with BUFFER_TIERS_ENABLED=false the function must
return LIMIT_BUFFER_TICKS unchanged, whatever the premium.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.order.buffer_tiers import (  # noqa: E402
    DEFAULT_FLOOR_TICKS,
    buffer_dollars_for_premium,
    resolve_buffer_ticks,
)


class TestTheTiersAsSpecified:
    """The bands the strategy defines, in dollars, before any floor."""

    @pytest.mark.parametrize(
        "premium,expected",
        [
            (0.01, 0.01),
            (0.28, 0.01),
            (1.49, 0.01),
            (1.50, 0.02),   # boundaries belong to the HIGHER band
            (2.49, 0.02),
            (2.50, 0.03),
            (3.49, 0.03),
            (3.50, 0.05),
            (5.99, 0.05),
            (6.00, 0.10),
            (12.00, 0.10),
        ],
    )
    def test_each_band(self, premium, expected):
        assert buffer_dollars_for_premium(premium) == expected

    def test_the_bands_never_decrease(self):
        """A dearer option must never get a smaller buffer."""
        previous = 0.0
        for premium in [0.5, 1.5, 2.5, 3.5, 6.0, 10.0]:
            buffer = buffer_dollars_for_premium(premium)
            assert buffer >= previous
            previous = buffer


class TestResolvingToTicks:
    def test_a_penny_tick_converts_exactly(self):
        ticks, reason = resolve_buffer_ticks(
            premium=4.00, tick_size=0.01, fallback_ticks=5,
            enabled=True, floor_ticks=1,
        )
        assert ticks == 5          # 0.05 / 0.01
        assert "premium tiers" in reason

    def test_the_cheapest_band_is_one_tick_without_a_floor(self):
        ticks, _ = resolve_buffer_ticks(
            premium=0.89, tick_size=0.01, fallback_ticks=5,
            enabled=True, floor_ticks=1,
        )
        assert ticks == 1

    def test_the_floor_lifts_the_cheapest_band(self):
        """Measured: at 1 tick, three of 55 real fills missed by a cent."""
        ticks, reason = resolve_buffer_ticks(
            premium=0.89, tick_size=0.01, fallback_ticks=5,
            enabled=True, floor_ticks=2,
        )
        assert ticks == 2
        assert "floor" in reason

    def test_the_floor_does_not_touch_higher_bands(self):
        ticks, reason = resolve_buffer_ticks(
            premium=7.00, tick_size=0.01, fallback_ticks=5,
            enabled=True, floor_ticks=2,
        )
        assert ticks == 10         # 0.10 / 0.01
        assert "floor" not in reason

    def test_a_coarser_tick_rounds_rather_than_floors(self):
        """At a 0.05 tick a 0.03 buffer must not silently become zero ticks."""
        ticks, _ = resolve_buffer_ticks(
            premium=3.00, tick_size=0.05, fallback_ticks=1,
            enabled=True, floor_ticks=0,
        )
        assert ticks == 1

    def test_the_default_floor_is_two(self):
        assert DEFAULT_FLOOR_TICKS == 2


class TestItCanBeSwitchedOff:
    """The point of keeping this separate. Off must mean OFF."""

    @pytest.mark.parametrize("premium", [0.10, 1.00, 2.00, 4.00, 9.00])
    def test_disabled_always_returns_the_flat_setting(self, premium):
        ticks, reason = resolve_buffer_ticks(
            premium=premium, tick_size=0.01, fallback_ticks=5, enabled=False,
        )
        assert ticks == 5
        assert "LIMIT_BUFFER_TICKS" in reason
        assert "tiers are off" in reason

    def test_disabled_honours_a_zero_flat_buffer(self):
        ticks, _ = resolve_buffer_ticks(
            premium=1.00, tick_size=0.01, fallback_ticks=0, enabled=False,
        )
        assert ticks == 0


class TestUnusableInput:
    @pytest.mark.parametrize("tick_size", [0, -0.01])
    def test_a_bad_tick_size_falls_back_rather_than_dividing(self, tick_size):
        ticks, reason = resolve_buffer_ticks(
            premium=2.00, tick_size=tick_size, fallback_ticks=3, enabled=True,
        )
        assert ticks == 3
        assert "unusable" in reason


class TestItIsWiredIn:
    def test_the_settings_exist(self):
        from api.service.core.config import Settings

        assert "buffer_tiers_enabled" in Settings.__dataclass_fields__
        assert "buffer_tier_floor_ticks" in Settings.__dataclass_fields__

    def test_the_route_resolves_the_buffer_before_the_bracket(self):
        """The buffer must be decided before the prices are built from it."""
        import inspect

        from api.routes import trade

        source = inspect.getsource(trade.prepare_trade)
        assert source.index("resolve_buffer_ticks(") < source.index(
            "calculate_bracket_from_percentages("
        )

    def test_the_response_explains_the_buffer(self):
        """A buffer that varies per trade is untraceable without this."""
        from api.schemas import TickDetail

        assert "buffer_reason" in TickDetail.model_fields
