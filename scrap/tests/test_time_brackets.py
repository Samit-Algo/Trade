"""Bracket percentages that follow the clock.

The feature must be switchable off, so the off case is tested as carefully
as the on case. The boundaries get the most attention: an order placed at
the moment a window switches must land in exactly one of them, and an order
outside the session must fall back to the configured default rather than
picking up a window that is not running.
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

#: The project root. Resolved once rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.core.time_brackets import (  # noqa: E402
    MARKET_TIMEZONE,
    TimeBracketError,
    format_windows,
    parse_start,
    parse_timezone,
    parse_windows,
    resolve,
    session_start,
)

KOLKATA = ZoneInfo("Asia/Kolkata")
SINGAPORE = ZoneInfo("Asia/Singapore")

#: The schedule the feature was asked for: wide for the opening stretch,
#: tight for the rest.
WINDOWS = parse_windows("45:20:20, *:5:10")


def at(hour, minute, *, day=23, month=9, year=2026, zone=KOLKATA):
    """Build one aware moment. Keeps the tests readable."""
    return datetime(year, month, day, hour, minute, tzinfo=zone)


def bracket(moment, *, start=None, zone=KOLKATA, windows=WINDOWS):
    """Resolve a moment to its (take_profit, stop_loss), or None."""
    active = resolve(
        moment, windows=windows, start=start, display_timezone=zone
    )
    return None if active is None else (active.take_profit, active.stop_loss)


# ---------------------------------------------------------------------------
# The market clock this module duplicates
# ---------------------------------------------------------------------------


def test_market_timezone_agrees_with_the_market_module():
    """The copy here must not drift from the one in market/data.py.

    It is duplicated rather than imported to keep config out of an import
    cycle, which is exactly the situation where a copy rots unnoticed.
    """
    from api.service.market.data import MARKET_TIMEZONE as CANONICAL

    assert MARKET_TIMEZONE == CANONICAL


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_windows_parse_in_order():
    windows = parse_windows("45:20:20, *:5:10")

    assert len(windows) == 2
    assert windows[0].minutes == 45
    assert (windows[0].take_profit, windows[0].stop_loss) == (20, 20)
    assert windows[1].is_final
    assert (windows[1].take_profit, windows[1].stop_loss) == (5, 10)


def test_more_than_two_windows_need_no_code_change():
    windows = parse_windows("15:30:25, 45:20:20, *:5:10")

    assert len(windows) == 3
    assert [w.minutes for w in windows] == [15, 45, None]


def test_windows_round_trip_through_formatting():
    assert parse_windows(format_windows(WINDOWS)) == WINDOWS


@pytest.mark.parametrize(
    "table, because",
    [
        ("45:20:20", "no * window, so the rest of the session is undefined"),
        ("*:5:10, 45:20:20", "* is not last, so what follows is unreachable"),
        ("45:20:20, 30:5:5, *:5:10", "minutes do not ascend"),
        ("45:20", "not three parts"),
        ("45:20:150", "stop loss must be under 100"),
        ("45:2000:20", "take profit must be at most 1000"),
        ("45:0:20", "take profit must be above zero"),
        ("0:20:20", "a window must last at least a minute"),
        ("abc:20:20", "minutes must be a number"),
        ("", "an empty table is a typo, not an intention"),
    ],
)
def test_bad_window_tables_are_refused(table, because):
    with pytest.raises(TimeBracketError):
        parse_windows(table)


def test_market_open_keyword_means_no_fixed_time():
    assert parse_start("MARKET_OPEN") is None
    assert parse_start("market_open") is None
    assert parse_start("") is None


def test_a_fixed_start_is_read_as_a_clock_time():
    assert parse_start("19:00") == time(19, 0)


@pytest.mark.parametrize("bad", ["7pm", "25:00", "noon"])
def test_bad_start_times_are_refused(bad):
    with pytest.raises(TimeBracketError):
        parse_start(bad)


def test_an_abbreviation_is_not_a_timezone():
    """IST is ambiguous -- India, Israel and Ireland all claim it."""
    with pytest.raises(TimeBracketError):
        parse_timezone("IST")


def test_iana_names_are_accepted():
    assert parse_timezone("Asia/Kolkata") == KOLKATA


# ---------------------------------------------------------------------------
# The boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour, minute, expected, because",
    [
        (18, 59, None, "before the open, so the default applies"),
        (19, 0, (20, 20), "the open itself is in the first window"),
        (19, 44, (20, 20), "the last minute of the first window"),
        (19, 45, (5, 10), "the switch belongs to the window it opens"),
        (23, 59, (5, 10), "still the same session"),
    ],
)
def test_each_boundary_lands_in_one_window(hour, minute, expected, because):
    assert bracket(at(hour, minute)) == expected, because


def test_before_the_open_is_not_window_one():
    """Pre-market is outside the schedule, not the start of it.

    Falling into window one here would apply the opening bracket hours
    before the open.
    """
    assert bracket(at(12, 0)) is None


def test_the_last_window_does_not_run_into_the_next_morning():
    """The * window runs to the close, not for ever.

    Without a session end it would still be "active" at breakfast, applying
    the previous session's bracket to a trade placed the next day.
    """
    assert bracket(at(1, 30, day=24)) is None
    assert bracket(at(8, 29, day=24)) is None


# ---------------------------------------------------------------------------
# Daylight saving -- the reason MARKET_OPEN exists
# ---------------------------------------------------------------------------


def test_market_open_follows_the_us_clock_change():
    """India never moves its clocks; the United States does.

    In September the US opens at 19:00 IST. In December it opens at 20:00
    IST. A start hardcoded to 19:00 would be half an hour adrift of the
    open for four months of the year -- which is what MARKET_OPEN avoids.
    """
    september = session_start(at(20, 0, month=9), None).astimezone(KOLKATA)
    december = session_start(at(20, 0, month=12, day=23), None).astimezone(KOLKATA)

    assert september.strftime("%H:%M") == "19:00"
    assert december.strftime("%H:%M") == "20:00"


def test_the_opening_window_tracks_the_open_in_december():
    """20:00 IST is the open in December, so it is window one then."""
    assert bracket(at(20, 0, month=12)) == (20, 20)
    assert bracket(at(20, 45, month=12)) == (5, 10)

    # The same clock time in September is already past the switch.
    assert bracket(at(20, 0, month=9)) == (5, 10)


def test_a_fixed_start_does_not_follow_the_us_clock():
    """Asked for a fixed time, the schedule stays where it was put."""
    for month in (9, 12):
        opened = session_start(at(20, 0, month=month), time(19, 0))
        assert opened.strftime("%H:%M") == "19:00"


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------


def test_the_same_instant_resolves_alike_in_two_zones():
    """A colleague in Singapore must get the bracket the trade really has.

    Only the displayed times differ: the underlying moment is one moment.
    """
    moment = at(19, 30)  # inside window one in Kolkata

    in_kolkata = resolve(
        moment, windows=WINDOWS, start=None, display_timezone=KOLKATA
    )
    in_singapore = resolve(
        moment, windows=WINDOWS, start=None, display_timezone=SINGAPORE
    )

    assert in_kolkata.take_profit == in_singapore.take_profit
    assert in_kolkata.stop_loss == in_singapore.stop_loss
    assert in_kolkata.starts_at.strftime("%H:%M") == "19:00"
    assert in_singapore.starts_at.strftime("%H:%M") == "21:30"


def test_a_fixed_start_crossing_local_midnight_stays_in_session():
    """At 00:30 a 19:00 start means yesterday evening's, still running.

    Taking today's date would put the start eighteen hours in the future
    and report a live session as "not started".
    """
    assert bracket(at(0, 30, day=24), start=time(19, 0)) == (5, 10)
    assert bracket(at(19, 0, day=24), start=time(19, 0)) == (20, 20)


# ---------------------------------------------------------------------------
# Explaining itself
# ---------------------------------------------------------------------------


def test_the_active_window_names_itself_for_the_order_record():
    active = resolve(
        at(19, 30), windows=WINDOWS, start=None, display_timezone=KOLKATA
    )

    label = active.label("Asia/Kolkata")

    assert "window 1 of 2" in label
    assert "19:00-19:45" in label
    assert "Asia/Kolkata" in label


def test_minutes_remaining_counts_down_and_stops_at_the_last_window():
    first = resolve(
        at(19, 30), windows=WINDOWS, start=None, display_timezone=KOLKATA
    )
    last = resolve(
        at(21, 0), windows=WINDOWS, start=None, display_timezone=KOLKATA
    )

    assert first.minutes_remaining == 15
    assert last.minutes_remaining is None, "the last window does not end"
