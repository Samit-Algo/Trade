"""Unit tests for the Phase 2 market data logic.

Everything here runs with no network and no credentials. The functions tested
are the pure ones: time conversion, spread arithmetic, liquidity, and the
regrouping of a flat chain into rows.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.market import (  # noqa: E402
    MarketDataError,
    calculate_spread,
    days_until_expiry,
    is_low_liquidity,
    milliseconds_to_date,
    parse_expiry_date,
    today_in_market_timezone,
)


class TestMillisecondsToDate:
    def test_converts_a_documented_expiry_timestamp(self):
        """Tiger's own example: 1547182800000 is the 2019-01-11 expiry."""
        assert milliseconds_to_date(1547182800000) == date(2019, 1, 11)

    def test_midnight_eastern_does_not_slip_a_day(self):
        """The timestamp is midnight US/Eastern; reading it as UTC gives the 12th."""
        converted = milliseconds_to_date(1547182800000)
        assert converted.day == 11


class TestParseExpiryDate:
    def test_parses_the_api_format(self):
        assert parse_expiry_date("2026-09-18") == date(2026, 9, 18)

    def test_rejects_a_different_format(self):
        with pytest.raises(MarketDataError, match="YYYY-MM-DD"):
            parse_expiry_date("18/09/2026")

    def test_rejects_nonsense(self):
        with pytest.raises(MarketDataError):
            parse_expiry_date("next friday")


class TestDaysUntilExpiry:
    def test_today_is_zero_days(self):
        assert days_until_expiry(today_in_market_timezone()) == 0

    def test_future_date_is_positive(self):
        future = today_in_market_timezone() + timedelta(days=25)
        assert days_until_expiry(future) == 25

    def test_past_date_is_negative(self):
        past = today_in_market_timezone() - timedelta(days=3)
        assert days_until_expiry(past) == -3


class TestCalculateSpread:
    def test_normal_spread(self):
        spread, spread_percent = calculate_spread(bid=5.00, ask=5.20)
        assert spread == pytest.approx(0.20)
        assert spread_percent == pytest.approx(3.846, abs=0.001)

    def test_missing_bid_gives_nothing(self):
        assert calculate_spread(None, 5.20) == (None, None)

    def test_missing_ask_gives_nothing(self):
        assert calculate_spread(5.00, None) == (None, None)

    def test_zero_ask_gives_no_percentage(self):
        """Nobody is offering the contract, so a percentage is meaningless."""
        spread, spread_percent = calculate_spread(bid=0.0, ask=0.0)
        assert spread == 0.0
        assert spread_percent is None


class TestIsLowLiquidity:
    def test_liquid_row_is_not_flagged(self):
        assert is_low_liquidity(volume=8900) is False

    def test_low_volume_is_flagged(self):
        assert is_low_liquidity(volume=2) is True

    def test_missing_data_is_treated_as_thin(self):
        assert is_low_liquidity(volume=None) is True

    def test_threshold_is_configurable(self):
        assert is_low_liquidity(50, threshold=10) is False
        assert is_low_liquidity(50, threshold=100) is True


