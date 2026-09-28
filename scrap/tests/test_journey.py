"""The life of one trade, derived from its bars.

The case this feature exists for is the near miss: price ran most of the way
to the take profit and came back, and realised P&L cannot tell you so. That
one gets the most attention here.

No network. Bars are handwritten, which is the point of keeping the
derivation pure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.market.bars import Bar, _rows_to_bars  # noqa: E402
from api.service.order import journey  # noqa: E402

#: 2026-09-24 20:11 UTC, the entry in the real trade this was built against.
START_MS = 1790280660000


def bar(minute, o, h, l, c, volume=10):
    """One bar, `minute` minutes after the entry."""
    return Bar(
        time_ms=START_MS + minute * 60_000,
        open=o, high=h, low=l, close=c, volume=volume,
    )


# ---------------------------------------------------------------------------
# The near miss -- the reason this exists
# ---------------------------------------------------------------------------


def test_a_peak_short_of_the_take_profit_is_measured():
    """Bought 0.97, take profit 1.05, reached 1.03 and fell away.

    Realised P&L would say this trade went nowhere. The journey has to say
    it got most of the way there, because that is what tells you the take
    profit is set too far out.
    """
    bars = [
        bar(0, 0.97, 0.99, 0.96, 0.98),
        bar(1, 0.98, 1.03, 0.98, 1.01),   # the peak
        bar(2, 1.01, 1.02, 0.95, 0.96),
    ]

    j = journey.build(
        bars, entry_price=0.97, take_profit_price=1.05, stop_loss_price=0.87
    )

    assert j.best.price == 1.03
    assert j.best.time_ms == START_MS + 60_000
    assert j.touched_take_profit is False, "it never actually reached 1.05"

    # 0.97 -> 1.03 of a 0.97 -> 1.05 journey is 6/8.
    assert j.progress_to_take_profit == pytest.approx(0.75)


def test_the_peak_comes_from_a_wick_not_a_close():
    """The high is the point. A minute that closed back at entry still
    reached what it reached, and a chart of closes would hide it."""
    bars = [bar(0, 1.00, 1.40, 0.99, 1.00)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=1.50)

    assert j.best.price == 1.40
    assert j.progress_to_take_profit == pytest.approx(0.8)


def test_high_water_is_reported_against_what_was_realised():
    """Up $62 at its best and closed at $28 is the story worth telling."""
    bars = [bar(0, 3.15, 3.46, 3.10, 3.20)]

    j = journey.build(bars, entry_price=3.15, quantity=2, multiplier=100)

    # (3.46 - 3.15) * 2 * 100
    assert j.high_water_pnl == pytest.approx(62.0)


# ---------------------------------------------------------------------------
# Reaching the levels
# ---------------------------------------------------------------------------


def test_touching_the_take_profit_is_recorded():
    bars = [bar(0, 1.00, 1.05, 1.00, 1.04)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=1.05)

    assert j.touched_take_profit is True
    assert j.progress_to_take_profit == pytest.approx(1.0)


def test_progress_is_capped_at_one():
    """Overshooting is still "got there", not 140% of got there."""
    bars = [bar(0, 1.00, 1.20, 1.00, 1.10)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=1.05)

    assert j.progress_to_take_profit == 1.0


def test_touching_the_stop_loss_is_recorded():
    bars = [bar(0, 1.00, 1.00, 0.90, 0.92)]

    j = journey.build(bars, entry_price=1.00, stop_loss_price=0.90)

    assert j.touched_stop_loss is True
    assert j.drawdown_to_stop_loss == pytest.approx(1.0)


def test_a_price_that_only_ever_fell_made_no_progress_upwards():
    """Not a negative fraction: it got nowhere towards the take profit."""
    bars = [bar(0, 1.00, 1.00, 0.80, 0.82)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=1.20)

    assert j.progress_to_take_profit == 0.0


# ---------------------------------------------------------------------------
# Not knowing, versus knowing it did nothing
# ---------------------------------------------------------------------------


def test_no_bars_means_unknown_rather_than_zero():
    """A missing chart must not report a flat trade.

    Zero would read as "it never moved", which is a different and wrong
    answer.
    """
    j = journey.build([], entry_price=1.00, take_profit_price=1.05)

    assert j.has_bars is False
    assert j.best is None
    assert j.worst is None
    assert j.progress_to_take_profit is None
    assert j.high_water_pnl is None


def test_no_take_profit_set_leaves_the_fraction_unanswered():
    bars = [bar(0, 1.00, 1.10, 1.00, 1.05)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=None)

    assert j.best.price == 1.10, "the peak is still known"
    assert j.progress_to_take_profit is None, "but there is nothing to measure it against"


def test_a_take_profit_at_the_entry_price_is_not_divided_by():
    bars = [bar(0, 1.00, 1.10, 1.00, 1.05)]

    j = journey.build(bars, entry_price=1.00, take_profit_price=1.00)

    assert j.progress_to_take_profit is None


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_bars_are_ordered_oldest_first_whatever_arrives():
    """Drawn left to right, so the order is not the caller's to get wrong."""
    bars = [bar(2, 1.0, 1.0, 1.0, 1.0), bar(0, 1.0, 1.0, 1.0, 1.0),
            bar(1, 1.0, 1.0, 1.0, 1.0)]

    j = journey.build(bars, entry_price=1.00)

    assert [b.time_ms for b in j.bars] == [
        START_MS, START_MS + 60_000, START_MS + 120_000
    ]


def test_a_single_bar_trade_still_has_a_peak():
    j = journey.build([bar(0, 1.00, 1.08, 0.97, 1.02)], entry_price=1.00)

    assert j.best.price == 1.08
    assert j.worst.price == 0.97


def test_the_percent_move_is_signed():
    j = journey.build([bar(0, 1.00, 1.50, 0.50, 1.00)], entry_price=1.00)

    assert j.best.percent_from_entry == pytest.approx(50.0)
    assert j.worst.percent_from_entry == pytest.approx(-50.0)


# ---------------------------------------------------------------------------
# Reading Tiger's frame
# ---------------------------------------------------------------------------


def test_an_empty_result_arrives_as_a_list_not_a_frame():
    """Tiger returns [] for a contract with no history, so testing .empty
    alone raises AttributeError -- the trap fetch_last_traded_close
    documents."""
    assert _rows_to_bars([]) == []
    assert _rows_to_bars(None) == []


def test_untraded_minutes_are_dropped():
    """A flat, volume-zero bar is a placeholder for a minute that did not
    trade. Keeping it draws a line through a gap at a price nobody quoted."""

    class Frame:
        columns = ["time", "open", "high", "low", "close", "volume"]
        def to_dict(self, _):
            return [
                {"time": START_MS, "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 1.0, "volume": 0},
                {"time": START_MS + 60_000, "open": 1.0, "high": 1.2,
                 "low": 1.0, "close": 1.1, "volume": 5},
            ]
        empty = False

    bars = _rows_to_bars(Frame())

    assert len(bars) == 1
    assert bars[0].high == 1.2


def test_one_malformed_row_does_not_lose_the_chart():
    class Frame:
        columns = ["time", "open", "high", "low", "close", "volume"]
        def to_dict(self, _):
            return [
                {"time": "nonsense", "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 1.0, "volume": 1},
                {"time": START_MS, "open": 1.0, "high": 1.2, "low": 1.0,
                 "close": 1.1, "volume": 5},
            ]
        empty = False

    assert len(_rows_to_bars(Frame())) == 1


# ---------------------------------------------------------------------------
# The window's run-up is context, not part of the trade
# ---------------------------------------------------------------------------


def test_the_peak_cannot_predate_the_entry():
    """The chart's window opens before the fill so the line has a run-up.

    Those bars are not the trade. A peak taken from them reports a price
    the position never had -- one real trade showed a "best reached" two
    minutes before it was opened, promising profit that was never
    reachable.
    """
    before = bar(0, 5.00, 5.50, 4.90, 5.00)   # run-up, higher than anything after
    owned = [bar(1, 1.00, 1.20, 0.95, 1.10), bar(2, 1.10, 1.15, 1.00, 1.05)]

    # What the route passes: only the bars from the fill onward.
    j = journey.build(owned, entry_price=1.00)

    assert j.best.price == 1.20, "the 5.50 before entry must not be the peak"
    assert j.best.time_ms == START_MS + 60_000

    # And the guard is the caller's: handed everything, it would pick 5.50.
    naive = journey.build([before] + owned, entry_price=1.00)
    assert naive.best.price == 5.50, (
        "build() measures what it is given -- the route is what must filter"
    )
