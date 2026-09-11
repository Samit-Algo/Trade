"""What happened to an order after it was sent.

Status, filled or not, what it actually cost, and cancelling. Read and cancel
only -- nothing in this file can open a position.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..core.broker import ORDERS_LIMITER
from .build import RULE_WIDTH, format_money
from .cost import CostEstimate


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
    """Turn whatever the SDK reports as a status into an uppercase name.

    The status arrives as an OrderStatus enum, its name, or its value, and the
    values are not the names -- OrderStatus.REJECTED is the string 'Inactive'
    and OrderStatus.NEW is 'Initial'. Comparing against the wrong one of those
    silently never matches, so everything is funnelled through here.

    This is for display and for deciding when to stop polling. It is never
    what determines whether anything filled.

    Args:
        raw_status: Whatever the Order object carried.

    Returns:
        An uppercase status name, or "UNKNOWN".
    """
    if raw_status is None:
        return "UNKNOWN"

    name = getattr(raw_status, "name", None)
    if name:
        return str(name).upper()

    text = str(raw_status).strip()

    # Map an enum *value* back to its name, e.g. 'Inactive' -> 'REJECTED'.
    try:
        from tigeropen.common.consts import OrderStatus

        for member in OrderStatus:
            if str(member.value).lower() == text.lower():
                return member.name.upper()
    except Exception:
        pass

    return text.upper().replace(" ", "_")


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


def _read_number(order, field_name: str, default=None):
    """Read a numeric attribute off an Order, tolerating absence."""
    value = getattr(order, field_name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_order_status(trade_client, order_id: int):
    """Fetch one order's current state from the broker.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The global order ID returned at submission.

    Returns:
        The Order object, or None if the broker returned nothing.
    """
    # get_order and get_orders share one wire method and one 120/min limit.
    ORDERS_LIMITER.wait()
    return trade_client.get_order(id=order_id)


def poll_until_settled(
    trade_client,
    order_id: int,
    requested_quantity: int,
    contract_multiplier: float,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
    announce: bool = True,
) -> FillOutcome:
    """Ask the broker what happened, repeatedly, until it stops changing.

    This exists because an order ID means the order was accepted for
    processing and nothing more. Between submission and settlement it may
    fill, part-fill, be rejected, or expire.

    Polling stops on a terminal status or when the attempts run out. Running
    out is reported honestly as "still working" -- not as failure, and
    certainly not as success.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to watch.
        requested_quantity: Contracts asked for.
        contract_multiplier: Shares per contract.
        poll_attempts: Maximum checks.
        poll_delay_seconds: Seconds between checks.
        announce: Whether to print progress.

    Returns:
        What the order actually did.
    """
    status = "UNKNOWN"
    filled_quantity = 0
    average_fill_price = None
    reason = ""
    attempts_used = 0
    reached_terminal = False

    for attempt in range(1, poll_attempts + 1):
        attempts_used = attempt

        if attempt > 1:
            time.sleep(poll_delay_seconds)

        order = get_order_status(trade_client, order_id)
        if order is None:
            if announce:
                print(f"  poll {attempt}/{poll_attempts}: broker returned nothing yet")
            continue

        status = normalise_status(getattr(order, "status", None))
        filled_quantity = int(_read_number(order, "filled", 0) or 0)
        average_fill_price = _read_number(order, "avg_fill_price", None)
        reason = str(getattr(order, "reason", "") or "")

        if announce:
            print(
                f"  poll {attempt}/{poll_attempts}: status={status} "
                f"filled={filled_quantity}/{requested_quantity}"
            )

        if is_terminal_status(status):
            reached_terminal = True
            break

    outcome = classify_fill(requested_quantity, filled_quantity)
    actual_cash = calculate_actual_cash(
        average_fill_price, filled_quantity, contract_multiplier
    )

    return FillOutcome(
        order_id=order_id,
        status=status,
        requested_quantity=requested_quantity,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        actual_cash=actual_cash,
        outcome=outcome,
        poll_attempts=attempts_used,
        reached_terminal_status=reached_terminal,
        reason=reason,
    )


def cancel_order(
    trade_client,
    order_id: int,
    requested_quantity: int = 0,
    contract_multiplier: float = 100.0,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
) -> FillOutcome:
    """Ask the broker to cancel an order, then find out whether it did.

    Cancellation is asynchronous, exactly like submission. A successful return
    confirms the request was accepted, not that the order is cancelled, so this
    polls afterwards rather than announcing success.

    A cancelled order may still have filled in part before it stopped. The
    outcome returned says how much.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to cancel.
        requested_quantity: The original quantity, for classifying the fill.
        contract_multiplier: Shares per contract, for the cash figure.
        poll_attempts: How many times to check the result.
        poll_delay_seconds: Seconds between checks.

    Returns:
        What the order ended up doing.
    """
    from ..core.broker import CANCEL_ORDER_LIMITER

    CANCEL_ORDER_LIMITER.wait()
    trade_client.cancel_order(id=order_id)

    print("  Cancel request accepted. That is not the same as cancelled.")
    print("  Asking the broker what actually happened...")

    return poll_until_settled(
        trade_client=trade_client,
        order_id=order_id,
        requested_quantity=requested_quantity,
        contract_multiplier=contract_multiplier,
        poll_attempts=poll_attempts,
        poll_delay_seconds=poll_delay_seconds,
    )
