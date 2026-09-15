"""The cache that lets a 1-second page poll a 60-per-minute broker.

get_positions is capped at 60 calls a minute -- exactly one a second, which
ONE open position would consume at a 1s poll. Past that the RateLimiter
sleeps, blocking the request thread rather than merely slowing the page. These
tests are about that arithmetic holding.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.core.live_cache import (  # noqa: E402
    ORDERS_MAX_AGE_SECONDS,
    POSITIONS_MAX_AGE_SECONDS,
    LiveCache,
)


class Counter:
    """A fetch that records how often it was really called."""

    def __init__(self, value="v"):
        self.calls = 0
        self.value = value

    def __call__(self):
        self.calls += 1
        return f"{self.value}-{self.calls}"


class TestItActuallyReducesCalls:
    """The whole point: many reads, few fetches."""

    def test_a_second_read_inside_the_window_does_not_fetch(self):
        cache, fetch = LiveCache(), Counter()

        cache.get("k", fetch, 10.0)
        cache.get("k", fetch, 10.0)

        assert fetch.calls == 1

    def test_it_refetches_once_the_value_is_stale(self):
        cache, fetch = LiveCache(), Counter()

        cache.get("k", fetch, 0.05)
        time.sleep(0.08)
        cache.get("k", fetch, 0.05)

        assert fetch.calls == 2

    def test_ten_rapid_reads_cost_one_fetch(self):
        """A page polling every second for ten seconds against a 3s cache
        must not make ten broker calls."""
        cache, fetch = LiveCache(), Counter()

        for _ in range(10):
            cache.get("k", fetch, 10.0)

        assert fetch.calls == 1

    def test_the_age_is_reported_so_a_caller_can_say_how_fresh(self):
        cache, fetch = LiveCache(), Counter()

        _value, first = cache.get("k", fetch, 10.0)
        time.sleep(0.05)
        _value, second = cache.get("k", fetch, 10.0)

        assert first == 0.0
        assert second >= 0.04


class TestKeysAreIndependent:
    """Refreshing positions must not block a reader asking for orders."""

    def test_two_keys_do_not_share_a_value(self):
        cache = LiveCache()
        positions, orders = Counter("pos"), Counter("ord")

        assert cache.get("positions", positions, 10.0)[0] == "pos-1"
        assert cache.get("orders", orders, 10.0)[0] == "ord-1"

    def test_invalidating_one_leaves_the_other(self):
        cache = LiveCache()
        positions, orders = Counter("pos"), Counter("ord")
        cache.get("positions", positions, 10.0)
        cache.get("orders", orders, 10.0)

        cache.invalidate("positions")
        cache.get("positions", positions, 10.0)
        cache.get("orders", orders, 10.0)

        assert positions.calls == 2
        assert orders.calls == 1


class TestInvalidation:
    """Placing or closing changes the account, so the display must not lag."""

    def test_invalidate_forces_the_next_read_to_fetch(self):
        cache, fetch = LiveCache(), Counter()
        cache.get("k", fetch, 10.0)

        cache.invalidate("k")
        cache.get("k", fetch, 10.0)

        assert fetch.calls == 2

    def test_invalidate_with_no_key_drops_everything(self):
        cache = LiveCache()
        a, b = Counter("a"), Counter("b")
        cache.get("a", a, 10.0)
        cache.get("b", b, 10.0)

        cache.invalidate()
        cache.get("a", a, 10.0)
        cache.get("b", b, 10.0)

        assert (a.calls, b.calls) == (2, 2)


class TestFailureDoesNotBlankThePage:
    """A page that flickers to an error on one bad tick is worse than one
    showing a number a few seconds old."""

    def test_a_failed_refresh_keeps_the_last_good_value(self):
        cache = LiveCache()
        cache.get("k", lambda: "good", 0.01)
        time.sleep(0.02)

        def broken():
            raise RuntimeError("broker said no")

        value, age = cache.get("k", broken, 0.01)

        assert value == "good"
        assert age > 0

    def test_a_failure_with_nothing_cached_raises(self):
        """There is nothing to fall back on, so the caller must hear about it
        rather than be handed None as though it were data."""
        cache = LiveCache()

        with pytest.raises(RuntimeError, match="broker said no"):
            cache.get("k", lambda: (_ for _ in ()).throw(RuntimeError("broker said no")), 10.0)


class TestConcurrentReadersFetchOnce:
    """Two browser tabs, or two threads of one, must not double the load."""

    def test_threads_racing_a_cold_cache_fetch_once(self):
        cache = LiveCache()
        calls = []

        def slow_fetch():
            calls.append(1)
            time.sleep(0.05)
            return "value"

        threads = [
            threading.Thread(target=lambda: cache.get("k", slow_fetch, 10.0))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(calls) == 1


class TestTheIntervalsFitTheBrokerLimits:
    """The numbers, checked against what Tiger actually allows."""

    def test_positions_stay_inside_sixty_a_minute(self):
        """get_positions is capped at 60/min. At a 3s refresh the ceiling is
        20 a minute however fast the page polls."""
        per_minute = 60 / POSITIONS_MAX_AGE_SECONDS

        assert per_minute <= 60

    def test_orders_stay_inside_their_limit(self):
        """get_orders is capped at 120/min."""
        per_minute = 60 / ORDERS_MAX_AGE_SECONDS

        assert per_minute <= 120

    def test_a_one_second_poll_would_not_fit_without_the_cache(self):
        """The reason this module exists, stated as a test."""
        uncached_per_minute = 60 / 1.0

        assert uncached_per_minute >= 60
        assert 60 / POSITIONS_MAX_AGE_SECONDS < uncached_per_minute
