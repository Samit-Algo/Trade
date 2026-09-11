"""Idempotency: making sure a retried request cannot become a second order.

`POST /trade` is the only way to place an order, and it can be retried -- by a
client that timed out, by a double-clicked button, by a network that dropped
the reply. This module is what makes that safe: a `client_order_id` is claimed
BEFORE the order can reach the broker, so a resend finds the claim and replays
the first outcome instead of buying twice.

`build_price_only_quote` sits here too. The library expects a QuoteSnapshot,
and the fast path has only one price to build it from.
"""


from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from api.service.market import QuoteSnapshot, QuoteSource


IN_FLIGHT = "IN_FLIGHT"
#: A request that finished, successfully or not, with a response to replay.
COMPLETE = "COMPLETE"


class RequestInFlight(Exception):
    """The same client_order_id is already being processed."""


@dataclass
class IdempotencyRecord:
    """One client_order_id, and what became of it."""

    state: str
    created_at: datetime
    response: dict | None = None


class IdempotencyStore:
    """Remembers client_order_ids so a retry cannot place a second order.

    The critical detail is WHEN the key is claimed: before the order reaches
    the broker, not after it returns. Claiming afterwards leaves a window in
    which a timed-out client resends, finds nothing recorded, and places a
    duplicate -- which is the exact failure this exists to prevent.

    A crash between claiming and completing therefore leaves the key stuck in
    IN_FLIGHT. That is deliberate. A stuck key refuses retries until it
    expires, and the caller recovers by reading GET /orders/{id}. Refusing a
    retry costs a missed trade; allowing one costs a duplicate position.

    In memory, so it dies with the process. Correct for a single-instance
    service and wrong for several behind a load balancer -- see HANDOVER 3d.
    """

    def __init__(self, ttl_seconds: int) -> None:
        """Create the store.

        Args:
            ttl_seconds: How long a finished request is remembered.
        """
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def claim(self, client_order_id: str) -> dict | None:
        """Reserve an id, or report what already happened to it.

        Args:
            client_order_id: The caller's unique key for this trade.

        Returns:
            None when the id is fresh and now claimed, or the stored response
            when this request has already completed.

        Raises:
            RequestInFlight: If an identical request is still running.
        """
        with self._lock:
            self._forget_expired()
            existing = self._records.get(client_order_id)

            if existing is None:
                self._records[client_order_id] = IdempotencyRecord(
                    state=IN_FLIGHT, created_at=datetime.now(timezone.utc)
                )
                return None

            if existing.state == IN_FLIGHT:
                raise RequestInFlight(
                    f"client_order_id {client_order_id!r} is already being "
                    "processed. Do not retry: an order may already be on the "
                    "book. Read GET /orders to find out."
                )

            return existing.response

    def complete(self, client_order_id: str, response: dict) -> None:
        """Record the outcome so a later retry replays it.

        Args:
            client_order_id: The key claimed earlier.
            response: The response body to replay verbatim.
        """
        with self._lock:
            self._records[client_order_id] = IdempotencyRecord(
                state=COMPLETE,
                created_at=datetime.now(timezone.utc),
                response=response,
            )

    def release(self, client_order_id: str) -> None:
        """Give an id back after a failure that placed NOTHING.

        Only safe when the request failed before the order could have reached
        the broker -- a validation error, a refused safety lock. Never call it
        once place_order has been attempted: if the call timed out, the order
        may exist, and releasing the key would let a retry place a second one.

        Args:
            client_order_id: The key to release.
        """
        with self._lock:
            record = self._records.get(client_order_id)
            if record is not None and record.state == IN_FLIGHT:
                del self._records[client_order_id]

    def _forget_expired(self) -> None:
        """Drop records older than the TTL. Caller holds the lock."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.ttl_seconds)
        for key in [
            key
            for key, record in self._records.items()
            if record.created_at < cutoff
        ]:
            del self._records[key]


def build_price_only_quote(entry_price: float) -> QuoteSnapshot:
    """Build the snapshot the library expects, from one price and nothing else.

    The sibling of build_quote_snapshot above. That one has five typed numbers
    to work with; this one has a single price, because POST /trade asks for a
    price and nothing else.

    bid and ask both carry that price so the arithmetic downstream stays
    consistent, and it is stamped MANUAL because that is what it is: a number a
    human read off a screen and a client forwarded.

    volume stays None deliberately. is_low_liquidity treats missing data as
    thin, so the absence fails safe instead of reading as "perfectly liquid".

    Args:
        entry_price: The price the order will actually be placed at.

    Returns:
        A QuoteSnapshot carrying that price as bid, ask and limit.
    """
    return QuoteSnapshot(
        bid=entry_price,
        ask=entry_price,
        volume=None,
        limit_price=entry_price,
        source=QuoteSource.MANUAL,
        captured_at=datetime.now(timezone.utc),
    )
