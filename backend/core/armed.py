"""The arming switch: whether an order may actually reach the broker.

WHAT THIS IS NOT. A second concept beside DRY_RUN. It IS DRY_RUN, made
flickable. Every guard that consulted `settings.dry_run` now asks `is_dry()`
instead, and there is still exactly one thing deciding whether an order is
sent. Adding a parallel flag would mean two answers to one question, and the
day they disagreed would be the day an order went out that should not have.

WHY IT EXISTS. `.env` is read once at startup, so changing DRY_RUN needed a
restart. That made "do not trade for the first ten minutes" into "kill the
server and start it again at 19:10" -- which also throws away the contract
cache and the idempotency store, and leaves no backend to answer the page.
Flipping a switch does the same job without any of that.

SAFE IS THE DEFAULT, AND STAYS THE DEFAULT. With nothing stored, .env
decides; with .env unset, dry run is true. Nothing here can make the service
armed by accident -- arming is always something somebody did.

WHAT THE SWITCH DOES NOT TOUCH. Closing a position. Nothing in the sell path
reads this, deliberately: a switch that stopped you selling what you already
hold would be dangerous in exactly the situation you most need out. Safe
means no NEW orders, not trapped.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from . import paths

#: In state/<market>/, gitignored: this machine's operating state, not part
#: of the project. One file per market -- see paths.state_file.
FILENAME = "armed_settings.json"


def settings_path(market_id: str = "US") -> Path:
    """This market's file."""
    return paths.state_file(market_id, FILENAME)


def _market_of(settings) -> str:
    """The market a settings object belongs to; US for one that predates markets."""
    return getattr(settings, "market_id", "US")

#: Reads and writes are serialised, as in symbol_settings.py. Two tabs
#: flipping at once would otherwise interleave a read-modify-write.
_lock = threading.RLock()


def read_stored(market_id: str = "US") -> bool | None:
    """Return one market's stored dry-run state, or None when nothing is stored.

    A missing or unreadable file is not an error: it means "nothing
    overridden", and .env decides. Refusing to trade because a convenience
    file is malformed would be worse than ignoring it -- but note which way
    that falls. Ignoring it leaves .env in charge, and .env defaults to dry
    run, so a corrupt file can only ever make this SAFER.

    Args:
        market_id: Which market's switch.

    Returns:
        True for dry run, False for armed, None when unset.
    """
    path = settings_path(market_id)
    with _lock:
        if not path.exists():
            return None

        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(stored, dict) or "dry_run" not in stored:
            return None

        return bool(stored["dry_run"])


def is_dry(settings) -> bool:
    """Whether orders are currently blocked from reaching the broker.

    THE ONE FUNCTION THE GUARDS CALL. Everything that used to read
    `settings.dry_run` asks this instead, so the page's switch and the
    startup value can never be read in different places.

    Args:
        settings: The market's loaded configuration: which market it is, and
            the .env fallback.

    Returns:
        True when nothing may be sent.
    """
    stored = read_stored(_market_of(settings))
    if stored is not None:
        return stored
    return settings.dry_run


def describe(settings) -> tuple[bool, str]:
    """Return the state and where it came from, for the page.

    Args:
        settings: The loaded configuration.

    Returns:
        A pair of (dry run, "the UI" or ".env").
    """
    stored = read_stored(_market_of(settings))
    if stored is not None:
        return stored, "the UI"
    return settings.dry_run, ".env"


def save(dry_run: bool, market_id: str = "US") -> bool:
    """Store the switch, overriding .env until it is cleared.

    Written to a temporary file and moved into place, so an interrupted
    write cannot leave a half-written file. A half-written file reads as
    absent, which falls back to .env -- the safe direction.

    Args:
        dry_run: True to block orders, False to arm.
        market_id: Which market's switch. Arming one arms no other.

    Returns:
        What was stored.
    """
    path = settings_path(market_id)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".armed_settings-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump({"dry_run": bool(dry_run)}, handle, indent=2)
            Path(handle.name).replace(path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    return bool(dry_run)


def clear(market_id: str = "US") -> None:
    """Forget one market's switch, so its settings file decides again.

    Deleting rather than writing a copy of .env restores .env as it is READ,
    including any later edit to it.
    """
    with _lock:
        settings_path(market_id).unlink(missing_ok=True)
