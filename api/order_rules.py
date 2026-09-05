"""The rules an order must pass before it can be submitted.

Two halves, both guarding the same doorway:

  1. Quote checks. The CLI asks a human for five numbers and re-prompts on a
     bad one. Over HTTP there is nobody to re-prompt, so the same checks run
     against the supplied values and a failure becomes a 400 naming which
     check failed. Nothing is weakened; only the recovery differs.
  2. Preview tokens. The HTTP replacement for typing the cash amount. A
     preview issues a single-use token; submitting presents that token and the
     exact cash figure. The prices are NOT resent, so a client cannot preview
     at one price and submit at another.

The decimal-slip check keeps its special status in both worlds: at the CLI it
demands the value retyped inside a phrase a reflexive "y" cannot clear; here it
demands an explicit boolean on a second, deliberate request.
"""


from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from api.service.market import LastTrade, calculate_spread, fetch_last_traded_close
from api.service.market import (
    QuoteSnapshot,
    QuoteSource,
    check_bid_below_ask,
    check_price_is_positive,
    decimal_slip_ratio,
    is_decimal_slip,
    is_limit_outside_spread,
    is_spread_suspiciously_wide,
)

from .errors import ApiError


def look_up_last_trade(quote_client, identifier: str) -> LastTrade | None:
    """Fetch the contract's last traded close for the decimal-slip check.

    Free: historical option bars need no quote entitlement.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: The contract identifier.

    Returns:
        The last trade, or None when the contract has never traded.
    """
    return fetch_last_traded_close(quote_client, identifier)


def check_ordinary_rules(quote_input) -> None:
    """Run the checks that would re-prompt at the CLI.

    Args:
        quote_input: The QuoteInput from the request body.

    Raises:
        ApiError: 400 naming the failed check.
    """
    problems = []

    for value, label in ((quote_input.bid, "bid"), (quote_input.ask, "ask"),
                         (quote_input.limit_price, "limit_price")):
        problem = check_price_is_positive(value, label)
        if problem:
            problems.append({"field": label, "problem": problem})

    ordering_problem = check_bid_below_ask(quote_input.bid, quote_input.ask)
    if ordering_problem:
        problems.append({"field": "bid/ask", "problem": ordering_problem})

    if problems:
        raise ApiError(
            status_code=400,
            error_code="QUOTE_REJECTED",
            message="The quote did not pass the sanity checks. Fix the fields "
                    "listed and submit again.",
            detail={"failed_checks": problems},
        )


def collect_quote_warnings(quote_input) -> list[str]:
    """Gather the things a human would have been asked to confirm.

    These do not block over HTTP. At the CLI they are yes/no prompts, and an
    API has no one to ask, so they are surfaced as warnings on the preview for
    a human to read before submitting.

    Args:
        quote_input: The QuoteInput from the request body.

    Returns:
        Warning lines, possibly empty.
    """
    warnings = []

    if is_spread_suspiciously_wide(quote_input.bid, quote_input.ask):
        _spread, spread_percent = calculate_spread(quote_input.bid, quote_input.ask)
        warnings.append(
            f"The spread is {spread_percent:.1f}% of the ask "
            f"({quote_input.bid:,.2f} / {quote_input.ask:,.2f}). That is very "
            "wide, and you pay it the moment you enter."
        )

    if is_limit_outside_spread(
        quote_input.limit_price, quote_input.bid, quote_input.ask
    ):
        warnings.append(
            f"The limit price {quote_input.limit_price:,.2f} is outside the "
            f"quoted market {quote_input.bid:,.2f} / {quote_input.ask:,.2f}. "
            "Legal, and occasionally deliberate, but usually a mistake."
        )

    return warnings


def check_for_decimal_slip(
    typed_ask: float,
    last_trade: LastTrade | None,
    override_confirmed: bool,
    multiplier: float,
) -> None:
    """Block a price that looks like a misplaced decimal point.

    The most expensive typing error available: it turns $520 into $5,200. It is
    its own failure mode rather than one warning among several, and clearing it
    takes a deliberate second request with confirm_price_override set.

    Args:
        typed_ask: The ask as supplied.
        last_trade: The contract's last traded close, or None.
        override_confirmed: Whether the client explicitly overrode.
        multiplier: Shares per contract, for the cash comparison.

    Raises:
        ApiError: 400 carrying the full evidence, when the ratio is off and no
            override was given.
    """
    if last_trade is None:
        # Nothing to compare against. The preview says so rather than staying
        # silent, because silence would imply the check passed.
        return

    ratio = decimal_slip_ratio(typed_ask, last_trade.close)
    if not is_decimal_slip(ratio):
        return

    if override_confirmed:
        return

    raise ApiError(
        status_code=400,
        error_code="PRICE_LOOKS_LIKE_DECIMAL_SLIP",
        message=(
            f"The ask you sent, {typed_ask:,.2f}, is {ratio:.1f}x the price this "
            f"contract last actually traded at ({last_trade.close:,.2f}). A "
            "misplaced decimal point is the likely explanation. At "
            f"{multiplier:,.0f} shares per contract this is the difference "
            f"between ${last_trade.close * multiplier:,.2f} and "
            f"${typed_ask * multiplier:,.2f}. If the price is genuinely right, "
            "send the request again with confirm_price_override set to true."
        ),
        detail={
            "typed_value": typed_ask,
            "last_close": last_trade.close,
            "last_close_date": last_trade.trade_date.isoformat(),
            "last_close_age_days": last_trade.days_old,
            "ratio": round(ratio, 4),
            "resubmit_with": {"confirm_price_override": True},
            "note": (
                "The last close can be days old on a thin contract, so this is "
                "a sanity check and not a price."
            ),
        },
    )


def build_quote_snapshot(quote_input, last_trade: LastTrade | None) -> QuoteSnapshot:
    """Turn a validated request body into the same snapshot the CLI produces.

    Stamped MANUAL because a human read these numbers off a screen and typed
    them into a client. Nothing about arriving over HTTP makes them fetched.

    Args:
        quote_input: The QuoteInput from the request body.
        last_trade: What the decimal-slip check compared against, for the audit.

    Returns:
        A QuoteSnapshot ready for the library.
    """
    ratio = decimal_slip_ratio(
        quote_input.ask, last_trade.close if last_trade else None
    )

    return QuoteSnapshot(
        bid=quote_input.bid,
        ask=quote_input.ask,
        volume=quote_input.volume,
        open_interest=quote_input.open_interest,
        limit_price=quote_input.limit_price,
        source=QuoteSource.MANUAL,
        captured_at=datetime.now(timezone.utc),
        last_close=last_trade.close if last_trade else None,
        last_close_date=last_trade.trade_date if last_trade else None,
        last_close_ratio=ratio,
    )



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


# ---------------------------------------------------------------------------
# Phase 10 -- idempotency for the single-call trading path
#
# The two-step token flow makes POST /orders safe to retry: redeeming a token
# deletes it, so a resend gets TOKEN_INVALID rather than a second order. A
# single-call endpoint has no token, so it needs its own answer, and the same
# guarantee: a retried request must never place a second order.
# ---------------------------------------------------------------------------

#: A request that has been accepted but whose outcome is not yet known.
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
