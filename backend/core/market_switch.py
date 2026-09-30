"""The page's on/off switch for a whole market, e.g. India.

WHY IT EXISTS. India reads OpenAlgo in the background -- the price recorder,
the exit watcher. On a day you trade only the US, OpenAlgo may not be running,
and every one of those reads failed loudly. Switching India off in the page
stops them all, with no restart and no file to edit.

WHAT "OFF" MEANS. The market is not ready: nothing contacts its broker, and a
trade for it is refused. Its settings and history files are untouched, and
switching it on again picks up where it left off.

ON IS THE DEFAULT. With nothing stored, a configured market is on -- exactly
as it was before this switch existed.

WHICH MARKETS EXIST AT ALL is a different question, answered by MARKETS in
config/server.env. This switch only turns on and off a market that exists.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from . import paths

#: In state/<market>/, gitignored, like the SAFE/ARMED switch.
FILENAME = "market_switch.json"

_lock = threading.RLock()


def settings_path(market_id: str) -> Path:
    """This market's file."""
    return paths.state_file(market_id, FILENAME)


def is_on(market_id: str) -> bool:
    """Whether a market is switched on. True when nothing is stored.

    A missing or unreadable file reads as on, which is how the market was
    before the switch existed.
    """
    path = settings_path(market_id)
    with _lock:
        if not path.exists():
            return True
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return True
        if not isinstance(stored, dict) or "on" not in stored:
            return True
        return bool(stored["on"])


def save(on: bool, market_id: str) -> bool:
    """Store the switch. Written to a temporary file and moved into place, so
    an interrupted write cannot leave half a file.

    Returns:
        What was stored.
    """
    path = settings_path(market_id)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".market_switch-", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                json.dump({"on": bool(on)}, handle, indent=2)
            Path(handle.name).replace(path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise
    return bool(on)
