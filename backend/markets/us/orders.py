"""Tiger's order objects: building them, reading them back, cancelling them.

Nothing here can place an order. That is submit.py, beside this file, and
every place_order in the project is in it.

The arithmetic these orders are built from -- the cost, the bracket prices,
the tick grid -- is neutral and lives in backend/services/order/.
"""

from __future__ import annotations

import time

from tigeropen.common.util.contract_utils import option_contract
from tigeropen.common.util.order_utils import (
    limit_order,
    limit_order_with_legs,
    order_leg,
)

from backend.services.order.build import DEFAULT_TIME_IN_FORCE
from backend.services.order.status import (
    DEFAULT_POLL_ATTEMPTS,
    DEFAULT_POLL_DELAY_SECONDS,
    FillOutcome,
    calculate_actual_cash,
    classify_fill,
    is_terminal_status,
)

from .broker import CANCEL_ORDER_LIMITER, ORDERS_LIMITER


def normalise_tiger_status(raw_status) -> str:
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


def build_option_order(
    settings,
    contract,
    action: str,
    quantity: int,
    limit_price: float,
    time_in_force: str = DEFAULT_TIME_IN_FORCE,
):
    """Construct the SDK order object for one option order.

    Builds and returns. Does not submit, and cannot: no submission call is
    imported here.

    The contract object is rebuilt from the identifier that Phase 3 already
    verified against Tiger. option_contract() is a pure local constructor that
    validates nothing, which is exactly right here -- verification happened
    earlier, and this step is assembly.

    Args:
        settings: Validated configuration, for the account number.
        contract: An OptionContractInfo, already verified.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        limit_price: The limit price to place at.
        time_in_force: Defaults to DAY. Paper accounts do not support GTC.

    Returns:
        The SDK Order object, unsent.
    """
    order_contract = option_contract(
        identifier=contract.identifier,
        multiplier=contract.multiplier,
    )

    # Market orders are deliberately not offered. Option spreads are wide and
    # thin contracts fill badly, so a market order here is a foot-gun.
    order = limit_order(
        account=settings.account,
        contract=order_contract,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )

    # Extended hours are deliberately off. Option liquidity outside regular
    # hours is far worse, and market and stop orders do not support it anyway.
    order.outside_rth = False

    return order


LEG_PROFIT = "PROFIT"


LEG_LOSS = "LOSS"


def build_option_order_with_bracket(
    settings,
    contract,
    action: str,
    quantity: int,
    limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str = DEFAULT_TIME_IN_FORCE,
    time_in_force: str = DEFAULT_TIME_IN_FORCE,
):
    """Construct a limit order with take-profit and stop-loss legs attached.

    Builds and returns. Does not submit.

    Both legs go in one list. The SDK encodes that as attach_type='BRACKETS',
    which the documented appendix lists as a valid attach type -- so the "only
    one sub-order" limit described in Tiger's app help does not apply to the
    API.

    Args:
        settings: Validated configuration, for the account number.
        contract: An OptionContractInfo, already verified.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        limit_price: The parent's limit price.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.
        leg_time_in_force: Time in force for the LEGS. Parameterised because
            whether a paper account accepts GTC on a leg is undocumented and
            could not be established without submitting; DAY is the SDK's own
            default and the conservative choice.
        time_in_force: Time in force for the parent. Paper rejects GTC.

    Returns:
        The SDK Order object, unsent, with both legs attached.
    """
    order_contract = option_contract(
        identifier=contract.identifier,
        multiplier=contract.multiplier,
    )

    take_profit_leg = order_leg(
        LEG_PROFIT,
        take_profit_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )
    stop_loss_leg = order_leg(
        LEG_LOSS,
        stop_loss_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )

    order = limit_order_with_legs(
        account=settings.account,
        contract=order_contract,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        order_legs=[take_profit_leg, stop_loss_leg],
        time_in_force=time_in_force,
    )

    # Extended hours off, as everywhere else in this project.
    order.outside_rth = False

    return order


def get_attached_legs(trade_client, parent_order_id: int) -> list:
    """Find the legs attached to a parent order.

    Tried two ways, because which one carries the legs is not documented:
    the parent's own `order_legs` attribute, and any order reporting this one
    as its `parent_id`.

    Args:
        trade_client: A tigeropen TradeClient.
        parent_order_id: The parent order's global ID.

    Returns:
        A list of dicts describing what was found, empty if nothing was.
    """
    from tigeropen.common.consts import Market

    found = []

    parent = get_order_status(trade_client, parent_order_id)
    if parent is not None:
        for leg in getattr(parent, "order_legs", None) or []:
            found.append(
                {
                    "source": "parent.order_legs",
                    "leg_type": getattr(leg, "leg_type", None),
                    "price": getattr(leg, "price", None),
                    "time_in_force": getattr(leg, "time_in_force", None),
                    "outside_rth": getattr(leg, "outside_rth", None),
                }
            )

    ORDERS_LIMITER.wait()
    try:
        recent_orders = trade_client.get_orders(limit=50, market=Market.US)
    except Exception:
        recent_orders = None

    for candidate in recent_orders or []:
        if getattr(candidate, "parent_id", None) == parent_order_id:
            found.append(
                {
                    "source": "child order",
                    "id": getattr(candidate, "id", None),
                    "order_type": getattr(candidate, "order_type", None),
                    "action": getattr(candidate, "action", None),
                    "quantity": getattr(candidate, "quantity", None),
                    "limit_price": getattr(candidate, "limit_price", None),
                    "aux_price": getattr(candidate, "aux_price", None),
                    "time_in_force": getattr(candidate, "time_in_force", None),
                    "status": normalise_tiger_status(getattr(candidate, "status", None)),
                    # What the leg actually sold at. A stop becomes a MARKET
                    # order once triggered, so this can differ from aux_price
                    # -- and that difference is the realised slippage.
                    "avg_fill_price": getattr(candidate, "avg_fill_price", None),
                    "filled": getattr(candidate, "filled", None),
                }
            )

    return found


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

        status = normalise_tiger_status(getattr(order, "status", None))
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
