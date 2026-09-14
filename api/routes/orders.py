"""Reading orders back: history, one order, and its attached legs.

Every route here is READ-ONLY. Placing an order is `POST /trade` and nothing
else, which is what makes this module safe to read without tracing what it
might send.

Handlers stay thin. Every decision -- what a status means, what actually
filled -- is made by the same library functions the CLI calls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from tigeropen.common.consts import SecurityType

from api.service.core.broker import ORDERS_LIMITER
from api.service.contract import parse_identifier
from api.service.order import (
    get_attached_legs,
    get_order_status,
    normalise_status,
)

from ..shared import get_settings, get_trade_client
from ..errors import ApiError
from ..schemas import (
    FillOutcomeOut,
    OrderHistoryResponse,
    OrderHistoryRow,
    OrderLegsResponse,
    WorkingOrderOut,
    shape_leg,
)

router = APIRouter(tags=["orders"])


def to_utc(milliseconds) -> datetime | None:
    """Turn one of Tiger's millisecond stamps into a datetime.

    Args:
        milliseconds: Epoch milliseconds, or None.

    Returns:
        An aware UTC datetime, or None when there was no stamp.
    """
    if not milliseconds:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


@router.get("/orders/history", response_model=OrderHistoryResponse)
def read_order_history(limit: int = Query(default=100, ge=1, le=300)):
    """List every order placed, newest first, saying what became of each.

    One Tiger call. Orders come back flat, with legs carrying a parent_id, so
    they are regrouped into families here and each family reduced to a single
    row plus its legs.

    Deliberately NOT the positions list. An order is a thing you did; a
    position is a thing you hold. A stopped-out order stays in this list
    forever and appears in no position.

    Args:
        limit: How many orders to ask Tiger for.

    Returns:
        The orders, with an outcome and realised P&L on each.
    """
    settings = get_settings()
    ORDERS_LIMITER.wait()
    raw = get_trade_client().get_orders(
        account=settings.account, sec_type=SecurityType.OPT, limit=limit
    )

    legs_by_parent: dict = {}
    parents = []
    manual_sells: dict = {}

    for order in raw or []:
        parent_id = getattr(order, "parent_id", None)
        if parent_id:
            legs_by_parent.setdefault(parent_id, []).append(order)
            continue

        # A standalone SELL is a manual close -- POST /positions/close sends
        # one, and so does selling by hand in the broker app. It has no
        # parent_id, so without this it would be listed as its OWN entry: a
        # "bought at" row showing the price it was SOLD at. It belongs to the
        # BUY it closes, which is matched by contract below.
        action = str(getattr(order, "action", "") or "").upper()
        if action == "SELL":
            contract_text = str(getattr(order, "contract", "")).split("/")[0]
            manual_sells.setdefault(contract_text, []).append(order)
            continue

        parents.append(order)

    rows = []
    for parent in parents:
        legs = legs_by_parent.get(getattr(parent, "id", None), [])
        identifier_for_match = str(getattr(parent, "contract", "")).split("/")[0]
        closes = manual_sells.get(identifier_for_match, [])
        outcome, note, exit_price = describe_outcome(parent, legs, closes)

        identifier = str(getattr(parent, "contract", "")).split("/")[0]
        # parse_identifier returns a 4-tuple, not an object. This is its
        # first real caller -- it was written in Phase 3 and marked dormant.
        try:
            underlying, expiry_text, put_call, strike = parse_identifier(identifier)
        except Exception:  # noqa: BLE001 - a malformed id must not hide the row
            underlying = identifier.split()[0] if identifier else "?"
            expiry_text = put_call = strike = None

        entry = getattr(parent, "avg_fill_price", None)
        quantity = float(getattr(parent, "quantity", 0) or 0)
        multiplier = 100.0

        pnl = pnl_percent = None
        if entry and exit_price:
            pnl = round((exit_price - entry) * multiplier * quantity, 2)
            pnl_percent = round((exit_price - entry) / entry * 100, 2)

        target = stop = tif = None
        for leg in legs:
            if "STP" in str(getattr(leg, "order_type", "")).upper():
                stop = read_leg_price(leg)
            else:
                target = read_leg_price(leg)
            tif = tif or str(getattr(leg, "time_in_force", "") or "") or None

        # Timing. Tiger stamps order_time when it accepted the order and
        # trade_time when it filled, so both ends are already on record --
        # nothing here is tracked by us, and it works for old orders too.
        placed_ms = getattr(parent, "order_time", None)
        filled_ms = getattr(parent, "trade_time", None)

        # The exit is whichever leg actually filled. Only one of the two can:
        # they are OCA, so the other is cancelled when the first triggers.
        exited_ms = None
        for leg in legs:
            if "FILLED" in str(getattr(leg, "status", "")).upper():
                leg_filled = getattr(leg, "trade_time", None)
                if leg_filled and (exited_ms is None or leg_filled < exited_ms):
                    exited_ms = leg_filled

        fill_delay = (
            round((filled_ms - placed_ms) / 1000.0, 1)
            if placed_ms and filled_ms
            else None
        )
        # Measured from the FILL, not from submission: this is how long the
        # position was actually held, which is what says whether the
        # take-profit and stop-loss levels are set sensibly.
        held = (
            round((exited_ms - filled_ms) / 1000.0, 1)
            if filled_ms and exited_ms
            else None
        )

        rows.append(
            OrderHistoryRow(
                order_id_text=str(getattr(parent, "id", "") or ""),
                placed_at=to_utc(placed_ms),
                identifier=identifier,
                underlying=underlying,
                strike=strike,
                option_type=put_call,
                expiry=expiry_text,
                action=str(getattr(parent, "action", "") or ""),
                quantity=quantity,
                limit_price=getattr(parent, "limit_price", None),
                fill_price=entry,
                status=str(getattr(parent, "status", "")).split(".")[-1],
                outcome=outcome,
                outcome_note=note,
                exit_price=exit_price,
                realised_pnl=pnl,
                realised_pnl_percent=pnl_percent,
                take_profit_price=target,
                stop_loss_price=stop,
                leg_time_in_force=tif,
                filled_at=to_utc(filled_ms),
                exited_at=to_utc(exited_ms),
                fill_delay_seconds=fill_delay,
                held_seconds=held,
                legs=[
                    WorkingOrderOut(
                        order_id_text=str(getattr(leg, "id", "") or ""),
                        action=str(getattr(leg, "action", "") or ""),
                        order_type=str(getattr(leg, "order_type", "") or "") or None,
                        price=read_leg_price(leg),
                        time_in_force=str(getattr(leg, "time_in_force", "") or "") or None,
                        status=str(getattr(leg, "status", "")).split(".")[-1] or None,
                        role=(
                            "STOP_LOSS"
                            if "STP" in str(getattr(leg, "order_type", "")).upper()
                            else "TAKE_PROFIT"
                        ),
                    )
                    for leg in legs
                ],
            )
        )

    rows.sort(key=lambda r: r.placed_at or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)

    return OrderHistoryResponse(
        orders=rows,
        took_profit=sum(1 for r in rows if r.outcome == "TOOK_PROFIT"),
        stopped_out=sum(1 for r in rows if r.outcome == "STOPPED_OUT"),
        closed_manually=sum(1 for r in rows if r.outcome == "CLOSED_MANUALLY"),
        still_open=sum(1 for r in rows if r.outcome == "STILL_OPEN"),
        total_realised_pnl=round(sum(r.realised_pnl or 0 for r in rows), 2),
    )


@router.get("/orders/{order_id}", response_model=FillOutcomeOut)
def read_order(order_id: int) -> FillOutcomeOut:
    """Report one order's current state.

    The status alone does not say what filled: an order marked CANCELLED or
    EXPIRED may still have filled in part. The outcome here is derived from
    the filled quantity.

    Args:
        order_id: The broker's global order ID.

    Returns:
        The order's state.
    """
    from api.service.order import calculate_actual_cash, classify_fill

    order = get_order_status(get_trade_client(), order_id)
    if order is None:
        raise ApiError(
            status_code=404,
            error_code="ORDER_NOT_FOUND",
            message=f"The broker returned nothing for order {order_id}.",
        )

    requested = int(getattr(order, "quantity", 0) or 0)
    filled = int(getattr(order, "filled", 0) or 0)
    average = getattr(order, "avg_fill_price", None)
    multiplier = float(getattr(getattr(order, "contract", None), "multiplier", 100) or 100)

    return FillOutcomeOut(
        order_id=order_id,
        status=normalise_status(getattr(order, "status", None)),
        outcome=classify_fill(requested, filled),
        requested_quantity=requested,
        filled_quantity=filled,
        average_fill_price=float(average) if average is not None else None,
        actual_cash=calculate_actual_cash(
            float(average) if average is not None else None, filled, multiplier
        ),
        settled=True,
        poll_attempts=1,
        broker_reason=str(getattr(order, "reason", "") or "") or None,
    )


@router.get("/orders/{order_id}/legs", response_model=OrderLegsResponse)
def read_order_legs(order_id: int) -> OrderLegsResponse:
    """Report the legs attached to a parent order.

    Legs are child orders carrying parent_id, not entries on the parent's
    order_legs attribute -- that stayed empty in every observation. Both routes
    are tried by the library; only child orders are returned here.

    Args:
        order_id: The parent order's global ID.

    Returns:
        The attached legs.
    """
    raw_legs = get_attached_legs(get_trade_client(), order_id)

    rows = []
    for raw_leg in raw_legs:
        if raw_leg.get("source") == "child order":
            rows.append(shape_leg(raw_leg))

    if rows:
        note = "Legs found as child orders carrying this order as their parent."
    else:
        note = (
            "No legs found. That does not by itself prove they were rejected: "
            "an order placed without a bracket has none, and legs are only "
            "visible once the parent has filled."
        )

    return OrderLegsResponse(parent_order_id=order_id, legs=rows, note=note)


def read_leg_price(order) -> float | None:
    """Read a leg's price from whichever field it uses.

    The two legs store their price in DIFFERENT fields: a take-profit is a
    LMT carrying limit_price, a stop-loss is a STP carrying aux_price.
    Reading the wrong one returns None and looks like a missing price.
    """
    return getattr(order, "limit_price", None) or getattr(order, "aux_price", None)


def describe_outcome(parent, legs, closes=()) -> tuple[str, str, float | None]:
    """Work out what became of one bracketed order.

    Tiger never says "the stop fired". It reports a status per order, and the
    story is in which LEG filled: a filled LMT is the target, a filled STP is
    the stop. Its partner shows CANCELLED with the reason "one of these OCA
    orders is filled".

    A position can also leave by a route the bracket knows nothing about: a
    manual SELL, from POST /positions/close or from the broker app. Those
    carry no parent_id and no leg role, so they are passed in separately --
    without them a closed position reads as STILL_OPEN forever, which is what
    happens when the legs expire unfilled and the close is done by hand.

    Args:
        parent: The entry order.
        legs: Its attached legs.
        closes: Standalone SELL orders on the same contract.

    Returns:
        A triple of (outcome code, a readable sentence, the exit fill price).
    """
    parent_status = str(getattr(parent, "status", "")).split(".")[-1].upper()
    filled = float(getattr(parent, "filled", 0) or 0)

    if filled <= 0:
        if "CANCEL" in parent_status:
            return "CANCELLED", "Cancelled before it filled. Nothing was bought.", None
        if "EXPIRE" in parent_status or "REJECT" in parent_status:
            return "EXPIRED", "Expired before it filled. Nothing was bought.", None
        return "NOT_FILLED", "Still waiting to fill. Nothing bought yet.", None

    for leg in legs:
        leg_status = str(getattr(leg, "status", "")).split(".")[-1].upper()
        if "FILLED" not in leg_status or float(getattr(leg, "filled", 0) or 0) <= 0:
            continue

        exit_price = getattr(leg, "avg_fill_price", None) or read_leg_price(leg)
        kind = str(getattr(leg, "order_type", "")).upper()

        if "STP" in kind:
            return (
                "STOPPED_OUT",
                f"STOP LOSS triggered. Sold at {exit_price:,.2f}.",
                exit_price,
            )
        return (
            "TOOK_PROFIT",
            f"TAKE PROFIT triggered. Sold at {exit_price:,.2f}.",
            exit_price,
        )

    # No leg fired. Was it closed by hand instead?
    for close in closes:
        close_status = str(getattr(close, "status", "")).split(".")[-1].upper()
        close_filled = float(getattr(close, "filled", 0) or 0)
        if "FILLED" not in close_status or close_filled <= 0:
            continue

        exit_price = getattr(close, "avg_fill_price", None) or read_leg_price(close)
        if exit_price is None:
            continue

        return (
            "CLOSED_MANUALLY",
            f"Closed by hand at {exit_price:,.2f}, not by either exit leg.",
            exit_price,
        )

    if legs:
        expired = [
            leg for leg in legs
            if "EXPIRE" in str(getattr(leg, "status", "")).upper()
        ]
        if len(expired) == len(legs) and legs:
            return (
                "STILL_OPEN",
                "Filled. Both exit legs EXPIRED unfilled -- DAY orders are "
                "cancelled at the session close, so this is unprotected.",
                None,
            )
        return (
            "STILL_OPEN",
            "Filled, and neither exit has triggered. You still hold this.",
            None,
        )
    return "STILL_OPEN", "Filled. No exits are attached to it.", None
