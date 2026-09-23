"""The clock-following schedule, as the page may have changed it.

TWO PLACES, ONE WINNER. .env holds the defaults. The page overrides them.
Whatever was saved from the page wins, because .env is read once at startup
and changing it needs a restart -- so the more recently changed value is the
one somebody meant. This is the same relationship symbol_settings.py has
with the per-symbol values in .env.

CLEARING IS NOT SETTING. `clear()` deletes the stored schedule rather than
writing a copy of the .env one. Saving a value and then clearing it gets you
back to .env exactly, including later edits to .env -- which writing a copy
would not.

STORED WHOLE, NOT FIELD BY FIELD. A schedule is one decision: a timezone, a
start and a table of windows that only make sense together. Overriding the
windows while leaving the start on .env would produce a schedule nobody
wrote, so the page saves all three or none.

VALIDATED ON THE WAY IN. Everything here goes through the same parsers .env
does, so a schedule that reached disk is one the trading path can read.
"""

from __future__ import annotations

import json
import tempfile
import threading
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import paths
from .time_brackets import (
    TimeBracketError,
    TimeWindow,
    format_windows,
    parse_start,
    parse_timezone,
    parse_windows,
)

#: Beside .env and symbol_settings.json, and gitignored for the same reason:
#: it is this machine's operating state, not part of the project.
SETTINGS_PATH = paths.TIME_BRACKET_SETTINGS_PATH

#: Reads and writes are serialised, as in symbol_settings.py. Two tabs saving
#: at once would otherwise interleave a read-modify-write and lose one.
_lock = threading.RLock()

_FIELDS = ("enabled", "timezone", "start", "windows")


@dataclass(frozen=True)
class Schedule:
    """A usable schedule, with everything already parsed.

    Attributes:
        enabled: Whether the clock is consulted at all.
        timezone_name: The IANA name, kept for messages and the page.
        timezone: The parsed zone.
        start_text: MARKET_OPEN or HH:MM, as written.
        start: The parsed clock time, or None for the US session open.
        windows: The parsed table, earliest first.
        source: "the UI" or ".env", so the page can say which is in force.
    """

    enabled: bool
    timezone_name: str
    timezone: ZoneInfo
    start_text: str
    start: time | None
    windows: tuple[TimeWindow, ...]
    source: str

    @property
    def windows_text(self) -> str:
        """The table in its .env form."""
        return format_windows(self.windows)


def read_stored() -> dict | None:
    """Return the saved schedule, unparsed.

    A missing or unreadable file is not an error. This is an overlay on top
    of .env, and its absence means "nothing overridden" -- refusing to trade
    because a convenience file is malformed would be worse than ignoring it.

    Returns:
        The stored fields, or None when nothing is stored.
    """
    with _lock:
        if not SETTINGS_PATH.exists():
            return None

        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(stored, dict):
            return None

        if not all(field in stored for field in _FIELDS):
            # A partial file cannot be completed from .env without inventing
            # a schedule nobody wrote. Treated as absent.
            return None

        return stored


def effective(settings) -> Schedule:
    """Return the schedule actually in force.

    The page's saved values if there are any, otherwise .env. A stored
    schedule that no longer parses -- hand-edited, or written by an older
    version -- falls back to .env rather than raising, for the same reason a
    malformed file does.

    Args:
        settings: The loaded configuration.

    Returns:
        The schedule, saying which of the two it came from.
    """
    stored = read_stored()

    if stored is not None:
        try:
            return _build(
                enabled=bool(stored["enabled"]),
                timezone_name=str(stored["timezone"]),
                start_text=str(stored["start"]),
                windows_text=str(stored["windows"]),
                source="the UI",
            )
        except TimeBracketError:
            pass

    return _build(
        enabled=settings.time_brackets_enabled,
        timezone_name=settings.time_brackets_timezone,
        start_text=settings.time_brackets_start,
        windows_text=settings.time_brackets_windows,
        source=".env",
    )


def _build(
    *, enabled: bool, timezone_name: str, start_text: str, windows_text: str, source: str
) -> Schedule:
    """Parse one schedule's four fields.

    Args:
        enabled: Whether the clock is consulted.
        timezone_name: An IANA name.
        start_text: MARKET_OPEN or HH:MM.
        windows_text: The window table.
        source: Where these came from, for the page.

    Returns:
        The parsed schedule.

    Raises:
        TimeBracketError: When any field is unusable.
    """
    return Schedule(
        enabled=enabled,
        timezone_name=timezone_name.strip(),
        timezone=parse_timezone(timezone_name),
        start_text=start_text.strip(),
        start=parse_start(start_text),
        windows=parse_windows(windows_text),
        source=source,
    )


def save(
    *, enabled: bool, timezone_name: str, start_text: str, windows_text: str
) -> Schedule:
    """Store a schedule, replacing whatever was there.

    Written to a temporary file and moved into place, so an interrupted
    write cannot leave a half-written file behind.

    Args:
        enabled: Whether the clock is consulted.
        timezone_name: An IANA name, e.g. Asia/Kolkata.
        start_text: MARKET_OPEN or a 24-hour HH:MM.
        windows_text: minutes:take_profit:stop_loss entries, ending in *.

    Returns:
        The schedule as stored.

    Raises:
        TimeBracketError: When any field is unusable. Nothing is written.
    """
    schedule = _build(
        enabled=enabled,
        timezone_name=timezone_name,
        start_text=start_text,
        windows_text=windows_text,
        source="the UI",
    )

    # Normalised on the way in, so the file holds what the parsers read back
    # rather than whatever spacing was typed.
    entry = {
        "enabled": schedule.enabled,
        "timezone": schedule.timezone_name,
        "start": schedule.start_text or "MARKET_OPEN",
        "windows": schedule.windows_text,
    }

    with _lock:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=SETTINGS_PATH.parent,
            prefix=".time_bracket_settings-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(entry, handle, indent=2, sort_keys=True)
            Path(handle.name).replace(SETTINGS_PATH)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    return schedule


def clear() -> None:
    """Forget the saved schedule, so .env comes back.

    Deleting rather than overwriting is deliberate: it restores .env as it
    is READ, including any later edit to it.
    """
    with _lock:
        SETTINGS_PATH.unlink(missing_ok=True)
