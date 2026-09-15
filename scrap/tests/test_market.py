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

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

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




class TestAnEmptyBarIsNotAPrice:
    """Tiger opens a bar the moment a minute begins, carrying the previous
    close forward with volume 0. Reading that reports a price nobody traded
    at, and it lags what the broker's own app shows.

    Measured live on QQQ:
        00:29  c=5.36  vol=21   <- the real last trade
        00:30  c=5.36  vol=0    <- carried forward, nothing happened
    """

    def bars(self, rows):
        import pandas

        return pandas.DataFrame(rows)

    def fetch(self, rows, monkeypatch):
        from api.service.market import data

        class FakeClient:
            def get_option_bars(self, **kwargs):
                return self.frame

        client = FakeClient()
        client.frame = self.bars(rows)
        monkeypatch.setattr(data.OPTION_BARS_LIMITER, "wait", lambda: None)
        return data.fetch_recent_traded_price(client, "QQQ   260918C00706000")

    def test_an_empty_newest_bar_is_skipped(self, monkeypatch):
        result = self.fetch([
            {"time": 1_000_000, "close": 5.36, "volume": 21},
            {"time": 1_060_000, "close": 5.36, "volume": 0},
        ], monkeypatch)

        assert result.volume == 21
        assert result.bar_time_ms == 1_000_000

    def test_several_empty_bars_are_walked_back_through(self, monkeypatch):
        """A contract that has not traded for minutes still reports its last
        REAL trade rather than the newest carried-forward close."""
        result = self.fetch([
            {"time": 1_000_000, "close": 5.20, "volume": 14},
            {"time": 1_060_000, "close": 5.20, "volume": 0},
            {"time": 1_120_000, "close": 5.20, "volume": 0},
            {"time": 1_180_000, "close": 5.20, "volume": 0},
        ], monkeypatch)

        assert result.bar_time_ms == 1_000_000

    def test_a_traded_newest_bar_is_used_as_is(self, monkeypatch):
        result = self.fetch([
            {"time": 1_000_000, "close": 5.20, "volume": 14},
            {"time": 1_060_000, "close": 5.41, "volume": 18},
        ], monkeypatch)

        assert result.price == 5.41

    def test_bars_with_no_volume_column_still_work(self, monkeypatch):
        """Volume is not guaranteed to be present. Absent, the newest bar is
        the best available answer -- unknown is not the same as zero."""
        result = self.fetch([
            {"time": 1_000_000, "close": 5.20},
            {"time": 1_060_000, "close": 5.41},
        ], monkeypatch)

        assert result.price == 5.41

    def test_the_age_is_measured_from_the_bar_that_traded(self, monkeypatch):
        """Not from the empty one -- otherwise a stale price would report
        itself as seconds old and read as current."""
        result = self.fetch([
            {"time": 1_000_000, "close": 5.36, "volume": 21},
            {"time": 1_060_000, "close": 5.36, "volume": 0},
        ], monkeypatch)

        assert result.bar_time_ms == 1_000_000
