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

from tiger_backend.market import (  # noqa: E402
    MarketDataError,
    OptionRow,
    StrikeRow,
    calculate_spread,
    days_until_expiry,
    find_atm_strike,
    is_low_liquidity,
    milliseconds_to_date,
    pair_calls_and_puts_by_strike,
    parse_expiry_date,
    select_strikes_around_price,
    today_in_market_timezone,
)


def make_option_row(
    strike: float,
    put_call: str,
    bid: float | None = 1.0,
    ask: float | None = 1.1,
    volume: int | None = 500,
    open_interest: int | None = 500,
) -> OptionRow:
    """Build an OptionRow for tests, with sensible liquid defaults."""
    spread, spread_percent = calculate_spread(bid, ask)
    return OptionRow(
        identifier=f"TEST {strike}{put_call}",
        strike=strike,
        put_call=put_call,
        multiplier=100,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        implied_volatility=0.25,
        spread=spread,
        spread_percent=spread_percent,
        is_thin=is_low_liquidity(volume, open_interest),
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
        assert is_low_liquidity(volume=8900, open_interest=41000) is False

    def test_low_volume_is_flagged(self):
        assert is_low_liquidity(volume=2, open_interest=41000) is True

    def test_low_open_interest_is_flagged(self):
        assert is_low_liquidity(volume=8900, open_interest=1) is True

    def test_missing_data_is_treated_as_thin(self):
        assert is_low_liquidity(volume=None, open_interest=500) is True
        assert is_low_liquidity(volume=500, open_interest=None) is True

    def test_threshold_is_configurable(self):
        assert is_low_liquidity(50, 50, threshold=10) is False
        assert is_low_liquidity(50, 50, threshold=100) is True


class TestPairCallsAndPutsByStrike:
    def test_pairs_a_call_and_put_at_the_same_strike(self):
        rows = [
            make_option_row(320.0, "CALL"),
            make_option_row(320.0, "PUT"),
        ]
        paired = pair_calls_and_puts_by_strike(rows)
        assert len(paired) == 1
        assert paired[0].call is not None
        assert paired[0].put is not None

    def test_sorts_strikes_ascending(self):
        rows = [
            make_option_row(330.0, "CALL"),
            make_option_row(310.0, "CALL"),
            make_option_row(320.0, "CALL"),
        ]
        paired = pair_calls_and_puts_by_strike(rows)
        strikes_in_order = [strike_row.strike for strike_row in paired]
        assert strikes_in_order == [310.0, 320.0, 330.0]

    def test_missing_side_stays_none(self):
        rows = [make_option_row(320.0, "CALL")]
        paired = pair_calls_and_puts_by_strike(rows)
        assert paired[0].call is not None
        assert paired[0].put is None

    def test_empty_input_gives_empty_output(self):
        assert pair_calls_and_puts_by_strike([]) == []


class TestFindAtmStrike:
    def make_rows(self, strikes: list[float]) -> list[StrikeRow]:
        return [StrikeRow(strike=strike, call=None, put=None) for strike in strikes]

    def test_picks_the_nearest_strike(self):
        rows = self.make_rows([300.0, 305.0, 310.0])
        assert find_atm_strike(rows, underlying_price=306.0) == 305.0

    def test_exact_match_wins(self):
        rows = self.make_rows([300.0, 305.0, 310.0])
        assert find_atm_strike(rows, underlying_price=310.0) == 310.0

    def test_price_below_every_strike(self):
        rows = self.make_rows([300.0, 305.0, 310.0])
        assert find_atm_strike(rows, underlying_price=100.0) == 300.0

    def test_empty_list_gives_none(self):
        assert find_atm_strike([], underlying_price=305.0) is None


class TestSelectStrikesAroundPrice:
    def make_rows(self, strikes: list[float]) -> list[StrikeRow]:
        return [StrikeRow(strike=strike, call=None, put=None) for strike in strikes]

    def test_takes_a_window_either_side(self):
        rows = self.make_rows([float(strike) for strike in range(100, 201, 5)])
        selected = select_strikes_around_price(rows, underlying_price=150.0, count_each_side=2)
        strikes = [strike_row.strike for strike_row in selected]
        assert strikes == [140.0, 145.0, 150.0, 155.0, 160.0]

    def test_window_is_clipped_at_the_start(self):
        """Near the bottom of the chain there is nothing below to show."""
        rows = self.make_rows([100.0, 105.0, 110.0, 115.0])
        selected = select_strikes_around_price(rows, underlying_price=100.0, count_each_side=2)
        strikes = [strike_row.strike for strike_row in selected]
        assert strikes == [100.0, 105.0, 110.0]

    def test_short_chain_is_returned_whole(self):
        rows = self.make_rows([100.0, 105.0])
        selected = select_strikes_around_price(rows, underlying_price=102.0, count_each_side=10)
        assert len(selected) == 2

    def test_empty_input_gives_empty_output(self):
        assert select_strikes_around_price([], underlying_price=100.0, count_each_side=5) == []
