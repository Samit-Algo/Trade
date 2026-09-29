"""Bracket percentages that follow the clock.

WHY. The minutes after the US open are the volatile ones. A 5% take-profit
that is sensible at noon is hit and closed in the first five minutes of the
session by noise, before the move it was waiting for has happened. So the
bracket widens for the opening stretch and tightens afterwards.

WHEN IT IS DECIDED. Once, when the order is built. The window is read at
entry and the position keeps the bracket it was born with -- nothing here
revisits an order that is already resting with the broker. There is no
monitoring loop in this service and this module does not add one.

WHY THE START IS "MARKET_OPEN" AND NOT A CLOCK TIME. India and Singapore
never move their clocks; the United States does, twice a year. A start
hardcoded to 19:00 IST is the US open today and is half an hour adrift of it
in November. MARKET_OPEN is resolved against the market's own calendar, so
the window tracks the real session without a seasonal edit. A fixed clock
time is still allowed for anyone who wants one.

WHAT THE WINDOWS MEAN. Minutes measured FROM the start, each running until
the next one begins:

    45:20:20, *:5:10

    minutes 0-45   take profit 20%, stop loss 20%
    minute 45 on   take profit 5%,  stop loss 10%

The final `*` is required. A table that stopped at 45 would say nothing
about the rest of the session, and guessing what was meant is worse than
refusing to start.

BEFORE THE START, NOTHING APPLIES. An order placed before the first window
opens is not window one -- it is outside the schedule, and the caller falls
back to the configured default. Pre-market is not the open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: US options trade on US Eastern time, and the session opens at 09:30 there.
#: Duplicated from market/data.py rather than imported: this module is on the
#: config import path, and `config -> market -> config` is a cycle. The two
#: are asserted to agree in tests/test_time_brackets.py.
MARKET_TIMEZONE = ZoneInfo("US/Eastern")
MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(16, 0)


@dataclass(frozen=True)
class Session:
    """When a market trades, on its own clock. What MARKET_OPEN resolves to."""

    timezone: ZoneInfo
    opens: time
    closes: time


#: The US options session. The default wherever no market is named, so every
#: caller written before there was more than one market means this.
US_SESSION = Session(MARKET_TIMEZONE, MARKET_OPEN_TIME, MARKET_CLOSE_TIME)

#: How long the schedule runs when the start is a fixed clock time rather
#: than MARKET_OPEN. A fixed start has no calendar to close against, so the
#: session is taken to be a normal trading day's length.
FIXED_SESSION_MINUTES = 390  # 09:30 to 16:00

#: What TIME_BRACKETS_START holds when it means "whenever the US session
#: opens" rather than a clock time.
MARKET_OPEN_KEYWORD = "MARKET_OPEN"

#: The same bounds .env and the per-symbol page are held to. A percentage
#: typed into the windows table cannot reach somewhere a configured one
#: could not.
TAKE_PROFIT_MAXIMUM = 1000.0
STOP_LOSS_MAXIMUM = 100.0


class TimeBracketError(Exception):
    """A window table or start time could not be used."""


@dataclass(frozen=True)
class TimeWindow:
    """One stretch of the session and the bracket it asks for.

    Attributes:
        minutes: Where this window ENDS, in minutes from the start. None for
            the final `*` window, which has no end.
        take_profit: Percent.
        stop_loss: Percent.
    """

    minutes: int | None
    take_profit: float
    stop_loss: float

    @property
    def is_final(self) -> bool:
        """Whether this is the `*` window that runs to the close."""
        return self.minutes is None


@dataclass(frozen=True)
class ActiveWindow:
    """Which window a given moment falls in, and how to explain it.

    Attributes:
        index: Position in the table, from zero.
        total: How many windows there are, for "window 1 of 2".
        window: The window itself.
        starts_at: When it opened, in the display timezone.
        ends_at: When it closes, or None for the final window.
        now: The moment this was resolved for, in the display timezone.
    """

    index: int
    total: int
    window: TimeWindow
    starts_at: datetime
    ends_at: datetime | None
    now: datetime

    @property
    def take_profit(self) -> float:
        return self.window.take_profit

    @property
    def stop_loss(self) -> float:
        return self.window.stop_loss

    @property
    def minutes_remaining(self) -> int | None:
        """Whole minutes until this window ends, or None if it does not."""
        if self.ends_at is None:
            return None
        return max(0, int((self.ends_at - self.now).total_seconds() // 60))

    def label(self, timezone_name: str) -> str:
        """Name this window the way the order's source string should read.

        Args:
            timezone_name: The display timezone, e.g. "Asia/Kolkata".

        Returns:
            Something like "time window 1 of 2, 19:00-19:45 Asia/Kolkata".
        """
        opened = self.starts_at.strftime("%H:%M")
        closed = self.ends_at.strftime("%H:%M") if self.ends_at else "close"
        return (
            f"time window {self.index + 1} of {self.total}, "
            f"{opened}-{closed} {timezone_name}"
        )


def parse_timezone(raw: str) -> ZoneInfo:
    """Turn a timezone name into a usable zone.

    Args:
        raw: An IANA name, e.g. "Asia/Kolkata".

    Returns:
        The zone.

    Raises:
        TimeBracketError: When the name is empty or unknown. Falling back to
            a guessed zone would shift every window by hours without saying
            so, so an unrecognised name is refused.
    """
    name = raw.strip()
    if not name:
        raise TimeBracketError(
            "The timezone is empty. Write an IANA name such as Asia/Kolkata "
            "or Asia/Singapore."
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise TimeBracketError(
            f"{name!r} is not a known timezone. Write an IANA name such as "
            "Asia/Kolkata or Asia/Singapore, not an abbreviation like IST."
        ) from error


def parse_start(raw: str) -> time | None:
    """Read the start time.

    Args:
        raw: Either MARKET_OPEN or a 24-hour "HH:MM".

    Returns:
        The clock time, or None meaning "the US session open".

    Raises:
        TimeBracketError: When it is neither.
    """
    text = raw.strip()
    if not text or text.upper() == MARKET_OPEN_KEYWORD:
        return None

    try:
        return time.fromisoformat(text)
    except ValueError as error:
        raise TimeBracketError(
            f"The start time {text!r} is not usable. Write {MARKET_OPEN_KEYWORD} "
            "to follow the US session open, or a 24-hour time such as 19:00."
        ) from error


def _check_percent(
    raw: str, *, piece: str, name: str, maximum: float, inclusive: bool
) -> float:
    """Validate one percentage from a window entry.

    Args:
        raw: The text to parse.
        piece: The whole entry, for the message.
        name: What this percentage is.
        maximum: Upper bound.
        inclusive: Whether `maximum` itself is allowed.

    Returns:
        The percentage.

    Raises:
        TimeBracketError: When it is not a number or out of range.
    """
    try:
        value = float(raw)
    except ValueError as error:
        raise TimeBracketError(
            f"Window {piece!r} has a {name} of {raw!r}, which is not a number."
        ) from error

    too_high = value > maximum if inclusive else value >= maximum
    if value <= 0 or too_high:
        limit = f"<= {maximum:g}" if inclusive else f"< {maximum:g}"
        raise TimeBracketError(
            f"Window {piece!r} has a {name} of {value:g}; it must be greater "
            f"than 0 and {limit}."
        )
    return value


def parse_windows(raw: str) -> tuple[TimeWindow, ...]:
    """Read the window table.

    Written as "minutes:take_profit:stop_loss" entries separated by commas,
    earliest first, the last one using `*` for its minutes:

        45:20:20, *:5:10

    Args:
        raw: The table.

    Returns:
        The windows, earliest first.

    Raises:
        TimeBracketError: When an entry is malformed, the minutes do not
            ascend, a `*` is not last, or there is no `*` at all. An
            out-of-order table would make later windows unreachable and a
            missing `*` would leave the rest of the session undefined, so
            both are refused rather than repaired.
    """
    entries = [piece.strip() for piece in raw.split(",") if piece.strip()]
    if not entries:
        raise TimeBracketError(
            "No windows are set. Write them as minutes:take_profit:stop_loss, "
            "e.g. 45:20:20, *:5:10."
        )

    windows: list[TimeWindow] = []
    for piece in entries:
        parts = piece.split(":")
        if len(parts) != 3:
            raise TimeBracketError(
                f"Window {piece!r} is not in the form "
                "minutes:take_profit:stop_loss, e.g. 45:20:20."
            )

        minutes_text, take_profit_text, stop_loss_text = (
            part.strip() for part in parts
        )

        if minutes_text == "*":
            minutes = None
        else:
            try:
                minutes = int(minutes_text)
            except ValueError as error:
                raise TimeBracketError(
                    f"Window {piece!r} has minutes of {minutes_text!r}. Write a "
                    "whole number of minutes from the start, or * for the last "
                    "window."
                ) from error
            if minutes <= 0:
                raise TimeBracketError(
                    f"Window {piece!r} ends at minute {minutes}; a window must "
                    "last at least one minute."
                )

        windows.append(
            TimeWindow(
                minutes=minutes,
                take_profit=_check_percent(
                    take_profit_text,
                    piece=piece,
                    name="take profit",
                    maximum=TAKE_PROFIT_MAXIMUM,
                    inclusive=True,
                ),
                stop_loss=_check_percent(
                    stop_loss_text,
                    piece=piece,
                    name="stop loss",
                    maximum=STOP_LOSS_MAXIMUM,
                    inclusive=False,
                ),
            )
        )

    for position, window in enumerate(windows[:-1]):
        if window.is_final:
            raise TimeBracketError(
                f"The * window is at position {position + 1} of "
                f"{len(windows)}. It runs to the close, so anything after it "
                "could never be reached -- put it last."
            )

    if not windows[-1].is_final:
        raise TimeBracketError(
            "The last window must be *, e.g. *:5:10. Without it the table "
            f"says nothing about the session after minute "
            f"{windows[-1].minutes}."
        )

    bounded = [window for window in windows if not window.is_final]
    for earlier, later in zip(bounded, bounded[1:]):
        if later.minutes <= earlier.minutes:
            raise TimeBracketError(
                f"Window minutes must ascend, but {later.minutes} follows "
                f"{earlier.minutes}. A window ending before the one before it "
                "can never be reached."
            )

    return tuple(windows)


def session_start(
    moment: datetime, start: time | None, session: Session = US_SESSION
) -> datetime:
    """Work out when the schedule begins on the day of `moment`.

    A session that began before local midnight still belongs to `moment`:
    at 01:15 in Kolkata a 19:00 start means YESTERDAY evening's, which is
    still running. Taking today's date would put the start eighteen hours in
    the future and report the live session as "not started".

    Args:
        moment: Any instant, in any zone. Its date in the RELEVANT zone
            decides which session is meant.
        start: A clock time in the display zone, or None for the market's
            open.
        session: The market whose open None means.

    Returns:
        The start, as an aware datetime.
    """
    if start is None:
        # The market's own calendar day, not the local one. At 19:00 in
        # Kolkata it is still the morning in New York, and it is that
        # morning's open the window belongs to.
        in_market = moment.astimezone(session.timezone)
        return datetime.combine(
            in_market.date(), session.opens, tzinfo=session.timezone
        )

    today = datetime.combine(moment.date(), start, tzinfo=moment.tzinfo)
    if moment < today:
        # Before today's start. Yesterday's may still be running.
        yesterday = today - timedelta(days=1)
        if moment < yesterday + timedelta(minutes=FIXED_SESSION_MINUTES):
            return yesterday

    return today


def session_end(
    opened: datetime, start: time | None, session: Session = US_SESSION
) -> datetime:
    """Work out when the session the schedule belongs to finishes.

    The final `*` window runs to the close, not for ever. Without an end,
    that window would still be reported as active the next morning.

    Args:
        opened: When the schedule began, aware.
        start: The configured start, to tell the two cases apart.
        session: The market whose close ends a MARKET_OPEN schedule.

    Returns:
        The close, as an aware datetime.
    """
    in_market = opened.astimezone(session.timezone)
    close = datetime.combine(in_market.date(), session.closes, tzinfo=session.timezone)
    if start is None:
        return close

    # A fixed start still ends at the market's close -- 15:30 for NSE, not
    # six and a half hours after a 09:30 start. Only a start placed after the
    # close, which has no close of its own that day, falls back to a normal
    # session's length.
    if close > opened:
        return close
    return opened + timedelta(minutes=FIXED_SESSION_MINUTES)


def resolve(
    moment: datetime,
    *,
    windows: tuple[TimeWindow, ...],
    start: time | None,
    display_timezone: ZoneInfo,
    session: Session = US_SESSION,
) -> ActiveWindow | None:
    """Find the window a moment falls in.

    Args:
        moment: When the order is being built. Aware; any zone.
        windows: The parsed table.
        start: A clock time in the display zone, or None for the market's open.
        display_timezone: The zone the caller reads times in.
        session: The market being traded, for what MARKET_OPEN means.

    Returns:
        The active window, or None when `moment` falls outside the session.
        None means "outside the schedule", and the caller should fall back
        to its configured default rather than assuming window one.
    """
    if not windows:
        return None

    local_now = moment.astimezone(display_timezone)
    opened = session_start(local_now, start, session).astimezone(display_timezone)

    if local_now < opened:
        return None

    # The last window has no end of its own, so without this it would still
    # be "active" at breakfast the next morning -- the previous session's
    # final window, hours after the market shut. The session closes it.
    if local_now >= session_end(opened, start, session):
        return None

    elapsed = (local_now - opened).total_seconds() / 60
    window_start = opened

    for index, window in enumerate(windows):
        if window.is_final or elapsed < window.minutes:
            ends_at = (
                None
                if window.is_final
                else opened + timedelta(minutes=window.minutes)
            )
            return ActiveWindow(
                index=index,
                total=len(windows),
                window=window,
                starts_at=window_start,
                ends_at=ends_at,
                now=local_now,
            )
        window_start = opened + timedelta(minutes=window.minutes)

    # Unreachable: the table is validated to end with a `*`, which matches
    # every elapsed time. Kept so a future change to parse_windows cannot
    # turn this into a silent None.
    return None


def format_windows(windows: tuple[TimeWindow, ...]) -> str:
    """Render a window table back into its .env form.

    Args:
        windows: The windows.

    Returns:
        Text that parse_windows would read back identically.
    """
    return ", ".join(
        f"{'*' if window.is_final else window.minutes}:"
        f"{window.take_profit:g}:{window.stop_loss:g}"
        for window in windows
    )
