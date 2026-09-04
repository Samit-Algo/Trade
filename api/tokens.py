"""Preview tokens: the HTTP replacement for typing the cash amount.

At the CLI a human sees the preview and types the cash figure back. Over HTTP
there is nobody to type, so the same guarantee is built from two parts:

  1. `POST /orders/preview` validates everything and hands back a token, an
     `expected_cash` figure, and the intent it will act on.
  2. `POST /orders` presents the token AND that exact cash figure.

**The prices are not resent when submitting.** The intent is held here, against
the token. If a client could resend prices, it could preview one price and
submit another, and the confirmation would be confirming nothing.

**Tokens are single use.** Redeeming one deletes it. That is what makes
`POST /orders` safe to retry: a client that times out and retries presents a
spent token and gets TOKEN_INVALID, never a second order.

Tokens live in memory only. A restart invalidates every outstanding preview,
which is correct -- a pending confirmation should not outlive the process that
made the promise.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: Long enough that guessing is not a strategy.
TOKEN_BYTES = 32


@dataclass(frozen=True)
class PreviewIntent:
    """Everything needed to submit an order, captured at preview time.

    Held server-side so the submit request carries only a token and a cash
    figure, and cannot restate the prices.
    """

    contract: Any
    quote: Any
    estimate: Any
    action: str
    quantity: int
    expected_cash: float
    take_profit_price: float | None
    stop_loss_price: float | None
    leg_time_in_force: str
    underlying_price: Any
    cash_available: float | None
    created_at: datetime
    expires_at: datetime

    @property
    def has_bracket(self) -> bool:
        """True when this order carries attached legs."""
        return self.take_profit_price is not None and self.stop_loss_price is not None


class TokenExpired(Exception):
    """The token was real but is past its expiry."""


class TokenNotFound(Exception):
    """No such token: never issued, already redeemed, or lost to a restart."""


class PreviewTokenStore:
    """Issues, redeems and expires preview tokens.

    Guarded by a lock because a redeem must be atomic. Two concurrent submits
    presenting the same token must not both succeed -- that is precisely the
    double-order this class exists to prevent.
    """

    def __init__(self, ttl_seconds: int) -> None:
        """Create the store.

        Args:
            ttl_seconds: How long an issued token stays valid.
        """
        self.ttl_seconds = ttl_seconds
        self._intents: dict[str, PreviewIntent] = {}
        self._lock = threading.Lock()

    def issue(self, **intent_fields) -> tuple[str, PreviewIntent]:
        """Store an intent and return the token that unlocks it.

        Args:
            **intent_fields: Everything PreviewIntent needs except the times.

        Returns:
            A pair of (token, the stored intent).
        """
        now = datetime.now(timezone.utc)
        intent = PreviewIntent(
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            **intent_fields,
        )
        token = secrets.token_urlsafe(TOKEN_BYTES)

        with self._lock:
            self._forget_expired(now)
            self._intents[token] = intent

        return token, intent

    def redeem(self, token: str) -> PreviewIntent:
        """Consume a token and return its intent.

        The token is deleted whether or not it had expired, so a retry after a
        timeout cannot place a second order.

        Args:
            token: The token from a preview response.

        Returns:
            The stored intent.

        Raises:
            TokenNotFound: If the token was never issued or is already spent.
            TokenExpired: If the token was valid but is now too old.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            intent = self._intents.pop(token, None)

        if intent is None:
            raise TokenNotFound(
                "That preview token is not valid. It was either already used, "
                "or it expired, or the service restarted. Request a new preview "
                "with POST /orders/preview and submit against that."
            )

        if now > intent.expires_at:
            age = (now - intent.created_at).total_seconds()
            raise TokenExpired(
                f"That preview expired {age:.0f} seconds ago, and prices move. "
                "Read the quote again and request a fresh preview."
            )

        return intent

    def _forget_expired(self, now: datetime) -> None:
        """Drop tokens that are past their expiry. Caller holds the lock.

        Args:
            now: The current time.
        """
        expired_tokens = [
            token
            for token, intent in self._intents.items()
            if now > intent.expires_at
        ]
        for token in expired_tokens:
            del self._intents[token]

    def outstanding_count(self) -> int:
        """Return how many unredeemed tokens are held. For diagnostics.

        Returns:
            The number of live tokens.
        """
        with self._lock:
            return len(self._intents)
