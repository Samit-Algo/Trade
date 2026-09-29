"""Per-symbol settings a human can change without restarting the server.

WHY A FILE AND NOT THE BROWSER. These decide whether an order is placed at
all, so the BACKEND has to hold them. Kept in localStorage they would exist
only in one browser: disabling a symbol would stop the /ui page offering it
while /trade, a script, or a curl went on trading it -- which is the opposite
of what "disabled" means.

WHY NOT .env. .env is read once at startup and changing it needs a restart.
These are meant to be flipped mid-session, in the page, between trades.

WHAT IT DOES NOT HOLD. Which symbols exist -- that stays TRADE_SYMBOLS in
.env. This file only enables, disables and tunes symbols that are already
configured; an entry for anything else is ignored rather than obeyed.

DISABLED MEANS NO NEW TRADES, NOT TRAPPED. Nothing here is consulted when
CLOSING a position. A symbol you have stopped opening trades on must still be
one you can sell.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from . import paths

#: In state/<market>/, gitignored: this machine's operating state, not part
#: of the project. One file per market -- see paths.state_file.
FILENAME = "symbol_settings.json"


def settings_path(market_id: str = "US") -> Path:
    """This market's file."""
    return paths.state_file(market_id, FILENAME)

#: Reads and writes are serialised. Two browser tabs saving at once would
#: otherwise interleave a read-modify-write and lose one of them.
_lock = threading.RLock()


class SymbolSettingsError(Exception):
    """A supplied setting could not be accepted."""


def _blank() -> dict:
    """What a symbol looks like before anyone has touched it."""
    return {"enabled": True, "take_profit": None, "stop_loss": None}


def read_all(market_id: str = "US") -> dict:
    """Return every stored symbol setting for one market.

    A missing or unreadable file is not an error. This is an overlay on top of
    .env, and its absence means "nothing overridden" -- refusing to start
    because a convenience file is malformed would be worse than ignoring it.

    Args:
        market_id: Which market's symbols.

    Returns:
        A mapping of symbol to its settings. Empty when nothing is stored.
    """
    path = settings_path(market_id)
    with _lock:
        if not path.exists():
            return {}

        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        if not isinstance(stored, dict):
            return {}

        return {
            str(symbol).upper(): {**_blank(), **value}
            for symbol, value in stored.items()
            if isinstance(value, dict)
        }


def read_for(symbol: str, market_id: str = "US") -> dict:
    """Return one symbol's settings, or the untouched defaults.

    Args:
        symbol: The underlying.
        market_id: The market it trades in.

    Returns:
        Its settings. Enabled, with no overrides, when nothing is stored.
    """
    return read_all(market_id).get(symbol.strip().upper(), _blank())


def is_enabled(symbol: str, market_id: str = "US") -> bool:
    """Whether new trades are allowed on this symbol.

    Defaults to True: a symbol in TRADE_SYMBOLS with nothing stored is
    tradable, so an empty settings file changes nothing.

    Args:
        symbol: The underlying.
        market_id: The market it trades in.

    Returns:
        False only when it has been explicitly disabled.
    """
    return bool(read_for(symbol, market_id).get("enabled", True))


def _check_percent(value, name: str, *, maximum: float, inclusive: bool):
    """Validate one percentage, or pass None through.

    The bounds are the SAME ones .env is held to, so a value typed into the
    page cannot reach somewhere a configured one could not.

    Args:
        value: The supplied value, or None to clear it.
        name: For the message.
        maximum: Upper bound.
        inclusive: Whether `maximum` itself is allowed.

    Returns:
        The value as a float, or None.

    Raises:
        SymbolSettingsError: When it is not a number or out of range.
    """
    if value is None or value == "":
        return None

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise SymbolSettingsError(f"{name} must be a number (got {value!r}).") from error

    too_high = number > maximum if inclusive else number >= maximum
    if number <= 0 or too_high:
        limit = f"<= {maximum:g}" if inclusive else f"< {maximum:g}"
        raise SymbolSettingsError(
            f"{name} must be greater than 0 and {limit} (got {number:g})."
        )

    return number


def save(
    symbol: str, *, enabled: bool, take_profit, stop_loss, market_id: str = "US"
) -> dict:
    """Store one symbol's settings, replacing whatever was there.

    Written to a temporary file and moved into place, so an interrupted write
    cannot leave a half-written file that the next read would discard --
    taking every other symbol's settings with it.

    Args:
        symbol: The underlying.
        enabled: False to refuse NEW trades on it. Closing is unaffected.
        take_profit: Percent, or None to fall back to .env.
        stop_loss: Percent, or None to fall back to .env.
        market_id: The market it trades in.

    Returns:
        The settings as stored.

    Raises:
        SymbolSettingsError: When a percentage is not usable.
    """
    wanted = symbol.strip().upper()

    entry = {
        "enabled": bool(enabled),
        "take_profit": _check_percent(
            take_profit, "take_profit", maximum=1000, inclusive=True
        ),
        "stop_loss": _check_percent(
            stop_loss, "stop_loss", maximum=100, inclusive=False
        ),
    }

    path = settings_path(market_id)
    with _lock:
        stored = read_all(market_id)
        stored[wanted] = entry

        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".symbol_settings-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(stored, handle, indent=2, sort_keys=True)
            Path(handle.name).replace(path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    return entry
