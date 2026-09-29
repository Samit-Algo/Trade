"""A short-lived cache in front of the broker, so a 1s page does not mean 1s of Tiger.

WHY THIS EXISTS. The order-history page polls every second so an open
position's P&L moves. Passed straight through, that is 60 get_positions calls
a minute -- exactly the documented limit, consumed by ONE open position. Two
positions, or two browser tabs, and the RateLimiter starts sleeping, which
blocks the request thread rather than merely slowing the poll.

WHAT IT CHANGES. Load stops depending on how much is held or how many tabs are
open. Ten positions and four tabs cost the same as one of each, because every
reader is served from whatever the last refresh fetched.

WHAT IT IS NOT. Not a store of truth. Nothing here decides anything -- orders
are placed and closed against live reads, exactly as before. This only feeds a
display, and the freshest it claims to be is stated in `age_seconds` so the
page can say so rather than implying a number is current.

THE CLOCK IS NOT CACHED. "Held for" is computed from the broker's own fill
timestamp every time it is asked, so it stays accurate to the second without
any fetch at all. That is the whole reason it moved off the browser: the page
used to stamp the fill when it first NOTICED one, which was wrong by up to a
poll interval and by however long the position had existed before anyone
opened the tab.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class _Entry:
    """One cached value, and when it was fetched."""

    value: Any = None
    fetched_at: float = 0.0
    error: Exception | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class LiveCache:
    """Serve a value from memory, refreshing it no faster than an interval.

    One lock per key, not one for the whole cache: refreshing positions must
    not block a reader asking for orders.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._guard = threading.Lock()

    def _entry(self, key: str) -> _Entry:
        with self._guard:
            if key not in self._entries:
                self._entries[key] = _Entry()
            return self._entries[key]

    def get(
        self, key: str, fetch: Callable[[], Any], max_age_seconds: float
    ) -> tuple[Any, float]:
        """Return the cached value, refreshing it if it has gone stale.

        A refresh that RAISES does not discard what is already held: a page
        that flickers to an error on one bad response is worse than one
        showing a number a few seconds old. The error is only raised when
        there is nothing cached to fall back on.

        Args:
            key: What is being cached.
            fetch: How to get it. Called at most once per max_age_seconds.
            max_age_seconds: How stale the value may be before a refresh.

        Returns:
            A pair of (the value, how many seconds old it is).

        Raises:
            Exception: Whatever `fetch` raised, but only when nothing has ever
                been cached for this key.
        """
        entry = self._entry(key)

        # The fast path: fresh enough, no lock, no fetch.
        age = time.monotonic() - entry.fetched_at
        if entry.fetched_at and age < max_age_seconds:
            return entry.value, age

        with entry.lock:
            # Another thread may have refreshed it while this one waited.
            age = time.monotonic() - entry.fetched_at
            if entry.fetched_at and age < max_age_seconds:
                return entry.value, age

            try:
                entry.value = fetch()
                entry.fetched_at = time.monotonic()
                entry.error = None
                return entry.value, 0.0
            except Exception as error:  # noqa: BLE001 -- see the docstring
                entry.error = error
                if entry.fetched_at:
                    return entry.value, time.monotonic() - entry.fetched_at
                raise

    def invalidate(self, key: str | None = None) -> None:
        """Drop a cached value so the next read fetches fresh.

        Called after anything that CHANGES the account -- placing an order,
        closing a position -- so the page does not show the old picture for
        up to an interval afterwards, which reads as the action having failed.

        Args:
            key: What to drop, or None for everything.
        """
        with self._guard:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)


#: One cache for the process. Every reader shares it, which is the point.
CACHE = LiveCache()

#: How stale each kind of data may be. Positions move constantly; the order
#: list changes only when something is placed or fills.
POSITIONS_MAX_AGE_SECONDS = 3.0
ORDERS_MAX_AGE_SECONDS = 5.0
