"""What happened to an order after it was sent.

Status, filled or not, what it actually cost, and cancelling. Read and cancel
only -- nothing in this file can open a position.
"""

from __future__ import annotations

from dataclasses import dataclass



#: How many times to ask the broker what happened before giving up.
DEFAULT_POLL_ATTEMPTS = 12

#: Seconds between polls. get_order allows 120/minute, so this is well
#: inside the limit while still settling most orders within a few seconds.
DEFAULT_POLL_DELAY_SECONDS = 2.0

#: The three outcomes that must be told apart.
NOTHING_FILLED = "NOTHING FILLED"
PARTIALLY_FILLED = "PARTIALLY FILLED"
FULLY_FILLED = "FULLY FILLED"

#: Statuses the broker will not move away from on its own. Reaching one
#: stops polling -- but never stops us reading the filled quantity.
TERMINAL_STATUSES = ("FILLED", "CANCELLED", "EXPIRED", "REJECTED")


# ---------------------------------------------------------------------------
# Phase 5 -- submission, and finding out what actually happened
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillOutcome:
    """What an order actually did, as opposed to what was asked of it."""

    order_id: int | None
    status: str
    requested_quantity: int
    filled_quantity: int
    average_fill_price: float | None
    actual_cash: float | None
    outcome: str  # NOTHING_FILLED, PARTIALLY_FILLED or FULLY_FILLED
    poll_attempts: int
    reached_terminal_status: bool
    reason: str  # the broker's own explanation, when it gives one

    @property
    def is_settled(self) -> bool:
        """True when the broker has stopped changing this order."""
        return self.reached_terminal_status

    def describe(self) -> str:
        """Return a one-line summary of what happened."""
        return (
            f"{self.outcome}: {self.filled_quantity} of "
            f"{self.requested_quantity} contract(s)"
        )


class OrderSubmissionError(Exception):
    """An order could not be submitted, or the human declined it."""


def normalise_status(raw_status) -> str:
    """Tidy a status into an upper-case name: FILLED, CANCELLED and so on.

    Each market translates its own broker's spellings first -- Tiger's, where
    REJECTED arrives as 'Inactive', in backend/markets/us/orders.py -- so what
    reaches this is already a name, or an enum carrying one.

    Args:
        raw_status: A status name, or an enum with a `name`.

    Returns:
        An uppercase status name, or "UNKNOWN".
    """
    if raw_status is None:
        return "UNKNOWN"
    name = getattr(raw_status, "name", None)
    if name:
        return str(name).upper()
    return str(raw_status).strip().upper().replace(" ", "_")


def is_terminal_status(status: str) -> bool:
    """Decide whether the broker has finished with this order.

    Args:
        status: A normalised status name.

    Returns:
        True when no further change is expected.
    """
    return status in TERMINAL_STATUSES


def classify_fill(requested_quantity: int, filled_quantity: int) -> str:
    """Decide which of the three outcomes happened.

    Deliberately takes only quantities. The status string plays no part: an
    order marked CANCELLED or EXPIRED may still have filled part way before it
    ended, and reporting that as "nothing happened" would be false.

    Args:
        requested_quantity: Contracts asked for.
        filled_quantity: Contracts actually filled.

    Returns:
        NOTHING_FILLED, PARTIALLY_FILLED or FULLY_FILLED.
    """
    if filled_quantity <= 0:
        return NOTHING_FILLED
    if filled_quantity >= requested_quantity:
        return FULLY_FILLED
    return PARTIALLY_FILLED


def calculate_actual_cash(
    average_fill_price: float | None,
    filled_quantity: int,
    multiplier: float,
) -> float | None:
    """Work out what the trade actually cost.

    From the average fill price and the filled quantity, never from the price
    asked for or the quantity requested. A partial fill at a different price is
    the normal case this exists to get right.

    Args:
        average_fill_price: What the fills averaged, per share.
        filled_quantity: Contracts actually filled.
        multiplier: Shares per contract.

    Returns:
        The cash that actually moved, or None when nothing filled.
    """
    if average_fill_price is None or filled_quantity <= 0:
        return None
    return round(average_fill_price * filled_quantity * multiplier, 2)

