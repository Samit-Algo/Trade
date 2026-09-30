"""The price of a held contract, recorded every couple of seconds.

WHY THIS EXISTS. Tiger's bars give one set of numbers per MINUTE. A fill,
an exit and a peak all happen inside a minute, so a line drawn from minute
closes had nowhere to put them: the entry dot floated off the line by
whatever the price did in the rest of that minute. The fix is not a better
way of drawing the minute -- it is having the prices in between.

WHERE THEY COME FROM. The position's own `market_price`, the number the
Tiger app shows. It needs no market-data entitlement and tracks continuously
(see position_market_price in routes/orders.py). It only exists while the
contract is HELD, which is exactly the span a trade journey draws.

ONE READ FOR EVERYTHING HELD. get_positions returns every position at once,
so ten open trades cost the same as one. It goes through the shared display
cache under the same key the history page uses, so the page's one-second
poll is served from what this fetched rather than adding calls of its own.

HOW OFTEN. get_positions is capped at 60 a minute, and placing an order
reads it too (symbol_lock.py). Every second would take the whole budget and
make an order wait behind this. Every two seconds takes half.

ON DISK. JSON lines, like the order audit log -- one folder per market,
one per contract, one file per UTC day:

    logs/prices/us/TSLA260928P00370000/2026-09-28.jsonl
    {"t": 1790620150000, "p": 3.6}
    {"t": 1790620152000, "p": 3.62}

A line is written only when the price CHANGES. A price that sits still for
a minute is one line, and the chart draws it flat until the next.

KEYED BY CONTRACT, NOT BY ORDER. The position list names the contract, not
the order that opened it. A journey already knows its fill and exit times,
so it reads the slice between them -- which also keeps two trades of the
same contract on one day apart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from backend.core import paths

#: How often the held prices are read while something is held. Half the
#: get_positions budget; see the module docstring.
RECORD_INTERVAL_SECONDS = 2.0

#: How often to look while nothing is held. A new fill is noticed within
#: this, and the chart's line starts at the fill price regardless.
IDLE_INTERVAL_SECONDS = 5.0

_log = logging.getLogger(__name__)


def _folder_name(identifier: str) -> str:
    """A contract identifier as a folder name.

    Tiger pads the root with spaces ("TSLA  260928P00370000"), which is a
    poor thing to put in a path. The identifier has no other separator, so
    dropping the spaces loses nothing.
    """
    return "".join(identifier.split())


def _day_file(identifier: str, day, root: Path) -> Path:
    return root / _folder_name(identifier) / f"{day.isoformat()}.jsonl"


class PriceLog:
    """Append-only price files, and reading a time slice back.

    Remembers the last price written per contract so an unchanged price is
    not written again. That memory starts empty, so after a restart the
    first reading is always written -- which is also what marks where the
    gap ended.
    """

    def __init__(self, root: Path | None = None, older: Path | None = None) -> None:
        self.root = root if root is not None else paths.PRICE_LOG_DIRECTORY
        # Read as well, never written: where prices went before each market
        # had a folder of its own, so a trade from then still draws.
        self.older = older
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, identifier: str, time_ms: int, price: float) -> bool:
        """Append one reading, unless the price has not moved.

        Args:
            identifier: The contract.
            time_ms: When the price was read, epoch milliseconds.
            price: The price.

        Returns:
            True when a line was written.
        """
        key = identifier.strip()
        with self._lock:
            if self._last.get(key) == price:
                return False

            day = datetime.fromtimestamp(time_ms / 1000, timezone.utc).date()
            path = _day_file(key, day, self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": int(time_ms), "p": price}) + "\n")

            self._last[key] = price
            return True

    def forget(self, held: Iterable[str]) -> None:
        """Drop the memory of contracts no longer held.

        So a contract bought again later starts a fresh run with its first
        price written, rather than skipping it for matching an old one.
        """
        keep = {h.strip() for h in held}
        with self._lock:
            for key in list(self._last):
                if key not in keep:
                    del self._last[key]

    def read(self, identifier: str, begin_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """The readings between two moments, oldest first.

        Args:
            identifier: The contract.
            begin_ms: From, inclusive, epoch milliseconds.
            end_ms: To, inclusive.

        Returns:
            (time_ms, price) pairs. Empty when nothing was recorded -- a
            trade from before the recorder existed, or one held while the
            backend was down.
        """
        if end_ms < begin_ms:
            return []

        first = datetime.fromtimestamp(begin_ms / 1000, timezone.utc).date()
        last = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()

        points: list[tuple[int, float]] = []
        day = first
        roots = [self.root] + ([self.older] if self.older is not None else [])
        files = []
        while day <= last:
            files += [_day_file(identifier, day, root) for root in roots]
            day += timedelta(days=1)

        for path in files:
            if path.exists():
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                            t, p = int(row["t"]), float(row["p"])
                        except (ValueError, KeyError, TypeError):
                            # A line cut short by a crash mid-write. The
                            # rest of the file is still good.
                            continue
                        if begin_ms <= t <= end_ms:
                            points.append((t, p))

        return sorted(set(points))


_LOGS: dict[str, PriceLog] = {}


def price_log(market_id: str) -> PriceLog:
    """One market's log, the same one for the whole process: its recorder
    writes it, the journey and the analysis read it.

    logs/prices/<market, lower case>/ -- US and NIFTY contracts kept apart.
    """
    key = market_id.strip().upper()
    if key not in _LOGS:
        _LOGS[key] = PriceLog(
            paths.PRICE_LOG_DIRECTORY / key.lower(), older=paths.PRICE_LOG_DIRECTORY
        )
    return _LOGS[key]


class PriceRecorder:
    """A background thread writing every held contract's price to the log.

    Args:
        read_positions: Returns (positions, age_seconds) for what is held
            now, each position carrying `identifier` and
            `market_price_latest`. Given max_age_seconds, the freshness
            wanted. Injected so this module never builds a broker client.
        log: Where to write.
        active: Asked before every read; while it says False nothing is
            read at all -- a market switched off, or outside its hours.
    """

    def __init__(
        self,
        read_positions: Callable[[float], tuple[list, float]],
        log: PriceLog | None = None,
        active: Callable[[], bool] | None = None,
    ) -> None:
        self._read_positions = read_positions
        self._log = log if log is not None else price_log("US")
        self._active = active
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="price-recorder", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def tick(self) -> int:
        """Read once and write whatever moved.

        Returns:
            How many contracts are held -- the caller sleeps longer when
            the answer is none.
        """
        if self._active is not None and not self._active():
            return 0
        positions, age = self._read_positions(RECORD_INTERVAL_SECONDS)
        # Stamped when the broker was READ, not now: a cached answer served
        # a second late describes the moment it was fetched.
        read_at_ms = int((time.time() - (age or 0.0)) * 1000)

        held = []
        for position in positions or []:
            identifier = str(getattr(position, "identifier", "") or "").strip()
            price = getattr(position, "market_price_latest", None)
            if not identifier:
                continue
            held.append(identifier)
            if price is None or price <= 0:
                continue
            self._log.record(identifier, read_at_ms, float(price))

        self._log.forget(held)
        return len(held)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                held = self.tick()
            except Exception:  # noqa: BLE001 -- one bad read must not end it
                _log.warning("price recorder: read failed", exc_info=True)
                held = 0
            wait = RECORD_INTERVAL_SECONDS if held else IDLE_INTERVAL_SECONDS
            self._stop.wait(wait)
