"""Positions and their profit and loss. Read-only."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query

from api.service.position import (
    DEFAULT_EXPIRY_WARNING_DAYS,
    list_option_positions,
    value_position,
)
from tigeropen.common.consts import SecurityType

from api.service.core.broker import OPEN_ORDERS_LIMITER, ORDERS_LIMITER
from api.service.market import (
    BidSnapshot,
    QuoteSource,
    fetch_recent_traded_price,
)

from ..shared import get_quote_client, get_settings, get_trade_client
from ..errors import ApiError
from ..schemas import (
    PositionDetailResponse,
    PositionsResponse,
    WorkingOrderOut,
)
from ..schemas import shape_position

router = APIRouter(tags=["positions"])


def parse_bid_arguments(bid_arguments: list[str]) -> dict[str, float]:
    """Parse repeated ?bid=IDENTIFIER:PRICE parameters.

    Args:
        bid_arguments: Raw values, each "IDENTIFIER:PRICE".

    Returns:
        A mapping of identifier to bid price.

    Raises:
        ApiError: 400 when a value is not in the expected shape.
    """
    bids = {}

    for raw in bid_arguments:
        identifier, separator, price_text = raw.rpartition(":")
        if not separator:
            raise ApiError(
                status_code=400,
                error_code="BID_MALFORMED",
                message=f"Could not read {raw!r} as a bid. Use the form "
                        "bid=IDENTIFIER:PRICE, e.g. "
                        "bid=AAPL  260918C00360000:0.27",
            )
        try:
            bids[identifier.strip()] = float(price_text)
        except ValueError:
            raise ApiError(
                status_code=400,
                error_code="BID_MALFORMED",
                message=f"{price_text!r} in {raw!r} is not a number.",
            ) from None

    return bids


@router.get("/positions", response_model=PositionsResponse)
def read_positions(
    bid: list[str] = Query(
        default=[],
        description="Optional, repeatable: IDENTIFIER:PRICE. A position can only "
        "be valued if you supply the current bid, because a position is worth "
        "what someone will PAY for it and this account cannot fetch quotes. "
        "Positions without a supplied bid come back with valuation null -- never "
        "a zero standing in for an unknown.",
    ),
    expiry_warning_days: int = Query(
        default=DEFAULT_EXPIRY_WARNING_DAYS,
        description="Flag positions with this many days left or fewer.",
    ),
) -> PositionsResponse:
    """List option positions, valuing any for which a bid was supplied.

    Args:
        bid: Repeated IDENTIFIER:PRICE values.
        expiry_warning_days: Threshold for the expiring_soon flag.

    Returns:
        The positions payload.
    """
    settings = get_settings()
    supplied_bids = parse_bid_arguments(bid)
    positions = list_option_positions(get_trade_client())

    rows = []
    total_cost_basis = 0.0
    total_current_value = 0.0
    total_unrealised = 0.0
    every_position_valued = True

    for position in positions:
        valuation = None
        bid_price = supplied_bids.get(position.identifier.strip())

        if bid_price is not None:
            # Stamped MANUAL because a human read this number off a screen.
            # Arriving over HTTP does not make it fetched.
            snapshot = BidSnapshot(
                bid=bid_price,
                source=QuoteSource.MANUAL,
                captured_at=datetime.now(timezone.utc),
            )
            valuation = value_position(
                position, snapshot, settings.quote_stale_after_seconds
            )
            total_current_value += valuation.current_value
            total_unrealised += valuation.unrealised_pnl
        else:
            every_position_valued = False

        row = shape_position(position, expiry_warning_days, valuation)
        total_cost_basis += row.cost_basis
        rows.append(row)

    return PositionsResponse(
        positions=rows,
        expiry_warning_days=expiry_warning_days,
        total_cost_basis=round(total_cost_basis, 2),
        total_current_value=(
            round(total_current_value, 2) if every_position_valued and rows else None
        ),
        total_unrealised_pnl=(
            round(total_unrealised, 2) if every_position_valued and rows else None
        ),
    )


def classify_working_order(action: str, order_type: str) -> str:
    """Say what an order resting on a contract is actually for.

    Tiger reports a side and an order type, not a purpose. A SELL stop on a
    long position is protection; a SELL limit is a target; a BUY is an entry
    that has not filled yet.

    Args:
        action: BUY or SELL.
        order_type: LMT, STP, STP_LMT and so on.

    Returns:
        ENTRY, TAKE_PROFIT, STOP_LOSS or OTHER.
    """
    side = (action or "").upper()
    kind = (order_type or "").upper()

    if side == "BUY":
        return "ENTRY"
    if side == "SELL" and "STP" in kind:
        return "STOP_LOSS"
    if side == "SELL" and "LMT" in kind:
        return "TAKE_PROFIT"
    return "OTHER"


def fetch_working_orders(identifier: str) -> list[WorkingOrderOut]:
    """List every order still live on the broker's book for one contract.

    Args:
        identifier: The full option identifier.

    Returns:
        The open orders, each labelled with what it is for.
    """
    settings = get_settings()
    OPEN_ORDERS_LIMITER.wait()
    raw = get_trade_client().get_open_orders(
        account=settings.account, sec_type=SecurityType.OPT
    )

    rows = []
    for order in raw or []:
        # Tiger renders the contract as "AAPL  260918C00360000/OPT/USD".
        contract_text = str(getattr(order, "contract", "")).split("/")[0]
        if contract_text != identifier:
            continue

        action = str(getattr(order, "action", "") or "")
        order_type = getattr(order, "order_type", None)
        rows.append(
            WorkingOrderOut(
                # A STRING on purpose. See HANDOVER 3g: these exceed 2^53 and
                # a JavaScript client silently rounds them.
                order_id_text=str(getattr(order, "id", "") or ""),
                action=action,
                order_type=str(order_type) if order_type else None,
                price=getattr(order, "limit_price", None)
                or getattr(order, "aux_price", None),
                time_in_force=str(getattr(order, "time_in_force", "") or "") or None,
                status=str(getattr(order, "status", "")).split(".")[-1] or None,
                role=classify_working_order(action, str(order_type or "")),
            )
        )
    return rows


def find_entry_fill_time(identifier: str) -> datetime | None:
    """Find when the BUY that opened this position filled.

    The hold clock starts at the fill, not at submission, so this reads
    Tiger's `trade_time` off the most recent filled BUY for the contract.
    Open orders do not carry it -- the parent has already filled and left
    the open-orders list -- so this looks at the order history instead.

    Args:
        identifier: The full option identifier.

    Returns:
        When the entry filled, or None when it cannot be determined.
    """
    settings = get_settings()
    ORDERS_LIMITER.wait()
    try:
        raw = get_trade_client().get_orders(
            account=settings.account, sec_type=SecurityType.OPT, limit=100
        )
    except Exception:  # noqa: BLE001 -- a missing clock must not hide a position
        return None

    newest = None
    for order in raw or []:
        contract_text = str(getattr(order, "contract", "")).split("/")[0]
        if contract_text != identifier:
            continue
        if str(getattr(order, "action", "")).upper() != "BUY":
            continue
        if "FILLED" not in str(getattr(order, "status", "")).upper():
            continue
        filled_ms = getattr(order, "trade_time", None)
        if filled_ms and (newest is None or filled_ms > newest):
            newest = filled_ms

    if newest is None:
        return None
    return datetime.fromtimestamp(newest / 1000, timezone.utc)


@router.get("/positions/detail", response_model=PositionDetailResponse)
def read_position_detail(identifier: str) -> PositionDetailResponse:
    """Price one held position live and report what is protecting it.

    Answers the question the positions list cannot: is there actually a stop
    resting on this, right now? A bracket whose legs were DAY and expired
    overnight leaves a position that looks fine in a holdings list and has no
    protection at all.

    Args:
        identifier: The full option identifier, e.g. "AAPL  260918C00360000".

    Returns:
        The position, its live P&L, and every order still working on it.

    Raises:
        ApiError: 404 when the position is not held.
    """
    positions = list_option_positions(get_trade_client())
    held = next((p for p in positions if p.identifier == identifier), None)

    if held is None:
        raise ApiError(
            status_code=404,
            error_code="POSITION_NOT_HELD",
            message=f"No open position for {identifier!r}.",
        )

    recent = fetch_recent_traded_price(get_quote_client(), identifier)

    cost_basis = held.average_cost * held.multiplier * held.quantity
    current_value = None
    pnl = None
    pnl_percent = None
    if recent is not None:
        current_value = round(recent.price * held.multiplier * held.quantity, 2)
        pnl = round(current_value - cost_basis, 2)
        pnl_percent = round(pnl / cost_basis * 100, 2) if cost_basis else None

    working = fetch_working_orders(identifier)
    has_stop = any(o.role == "STOP_LOSS" for o in working)
    has_target = any(o.role == "TAKE_PROFIT" for o in working)

    if has_stop and has_target:
        note = "Both exits are live on the book."
    elif has_stop:
        note = "A stop is live, but there is no take-profit."
    elif has_target:
        note = "A take-profit is live, but THERE IS NO STOP LOSS."
    else:
        note = (
            "NOTHING IS PROTECTING THIS POSITION. If its legs were DAY they "
            "expired at the close -- Tiger reports that as 'Rejected'."
        )

    # When did this position open, and how far is it from each exit?
    entry_filled_at = find_entry_fill_time(identifier)
    held_seconds = (
        round((datetime.now(timezone.utc) - entry_filled_at).total_seconds(), 1)
        if entry_filled_at
        else None
    )

    take_profit_price = next(
        (o.price for o in working if o.role == "TAKE_PROFIT" and o.price), None
    )
    stop_loss_price = next(
        (o.price for o in working if o.role == "STOP_LOSS" and o.price), None
    )

    # Distance from the CURRENT price, so it answers "how close am I now?".
    # Positive means the price still has to travel; the stop figure is the
    # cushion left before it triggers.
    percent_to_take_profit = percent_to_stop_loss = None
    if recent is not None and recent.price:
        if take_profit_price:
            percent_to_take_profit = round(
                (take_profit_price - recent.price) / recent.price * 100, 2
            )
        if stop_loss_price:
            percent_to_stop_loss = round(
                (recent.price - stop_loss_price) / recent.price * 100, 2
            )

    return PositionDetailResponse(
        identifier=held.identifier,
        underlying=held.underlying,
        strike=held.strike,
        option_type=held.put_call,
        expiry=held.expiry_date_text,
        days_to_expiry=held.days_to_expiry,
        quantity=held.quantity,
        multiplier=held.multiplier,
        average_cost=held.average_cost,
        cost_basis=round(cost_basis, 2),
        current_price=recent.price if recent else None,
        price_age_seconds=recent.age_seconds if recent else None,
        current_value=current_value,
        unrealised_pnl=pnl,
        unrealised_pnl_percent=pnl_percent,
        working_orders=working,
        entry_filled_at=entry_filled_at,
        held_seconds=held_seconds,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        percent_to_take_profit=percent_to_take_profit,
        percent_to_stop_loss=percent_to_stop_loss,
        has_stop_loss=has_stop,
        has_take_profit=has_target,
        protection_note=note,
    )
