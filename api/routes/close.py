"""Closing a position: POST /positions/close.

WHY THIS EXISTS. The bracket legs are the intended exit, but they are DAY
orders by default: the exchange cancels them at the session close and the
position is left holding nothing. Without this route there is no way to sell
through the API at all, which is exactly the wrong thing to discover while
holding an unprotected position.

WHAT IT DOES NOT DECIDE. Not the contract, not the size, not the price. The
contract and size come from the position being closed, and the price comes
from the caller. Closing is not a fresh trade decision; it is the reversal of
one already made.

THE SAME GUARD RUNS. sell_option calls assert_order_allowed immediately before
place_order, exactly as the buy path does. A close is still an order.

ONE THING THIS GETS RIGHT THAT THE FIRST ATTEMPT DID NOT. Everything that can
fail is done BEFORE submission. Once the order is sent, the response is built
from values already in hand -- because an exception after place_order returns
500 on an order that actually went through, and a caller who retries that
sells twice. It happened. See the FAILURE AFTER SUBMISSION note below.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.service.contract import find_option_contract
from api.service.core.safety import build_order_record, write_order_record
from api.service.order import sell_option, snap_down
from api.service.position import list_option_positions

from ..errors import ApiError
from ..order_rules import build_price_only_quote
from ..schemas import ClosePositionRequest, ClosePositionResponse
from ..shared import get_quote_client, get_settings, get_trade_client

router = APIRouter(tags=["positions"])


def find_position(identifier: str, trade_client):
    """Find the held position this identifier names.

    Args:
        identifier: The full option identifier.
        trade_client: A tigeropen TradeClient.

    Returns:
        The OptionPosition.

    Raises:
        ApiError: 404 when nothing matching is held. Closing something that is
            not held is never what the caller meant, so it is refused rather
            than sent as a short sale.
    """
    wanted = identifier.strip()

    for position in list_option_positions(trade_client):
        if position.identifier.strip() == wanted:
            return position

    raise ApiError(
        status_code=404,
        error_code="POSITION_NOT_HELD",
        message=(
            f"No position is held for {wanted!r}, so there is nothing to "
            "close. GET /positions lists what is held -- the identifier must "
            "match one of them exactly."
        ),
    )


def resolve_sell_limit(requested: float, tick_size: float) -> float:
    """Snap a sell limit onto a valid tick, and refuse a worthless one.

    Snapped DOWN, not to the nearest: this is a limit to SELL, and rounding it
    up would ask for more than the caller chose and risk not filling. Down is
    the direction that still fills.

    Args:
        requested: The limit the caller asked for.
        tick_size: The valid price increment.

    Returns:
        The limit, on the grid.

    Raises:
        ApiError: 422 when the result is not above zero. A quick-sell step can
            price below zero on a cheap option -- 0.10 below a 0.02 premium --
            and an order at or under zero is not a price.
    """
    limit = snap_down(requested, tick_size)

    if limit <= 0:
        raise ApiError(
            status_code=422,
            error_code="SELL_PRICE_NOT_POSITIVE",
            message=(
                f"A limit of {requested:.4f} snaps to {limit:.2f}, which is "
                "not a price. On a cheap option a large step can fall through "
                "zero -- choose a smaller one."
            ),
        )

    return limit


@router.post("/positions/close", response_model=ClosePositionResponse)
def close_position(body: ClosePositionRequest, request: Request) -> ClosePositionResponse:
    """Sell a held option position, at a limit price the caller supplies.

    The quantity is the whole position unless a smaller one is given. The
    price is required and has no default: this account cannot fetch an option
    bid, so nothing here knows what the contract is worth, and a market order
    on a thin option is how a position gets closed at a price nobody intended.

    Args:
        body: Which position, at what price, and how much of it.
        request: For the client address in the audit log.

    Returns:
        What the sell order actually did.

    Raises:
        ApiError: 404 if not held, 422 if the quantity exceeds the position or
            the price is not usable.
    """
    settings = get_settings()
    trade_client = get_trade_client()

    # ---- everything that can fail goes here, BEFORE anything is sent -------

    position = find_position(body.identifier, trade_client)
    held = int(abs(position.quantity))

    quantity = body.quantity if body.quantity is not None else held
    if quantity > held:
        raise ApiError(
            status_code=422,
            error_code="QUANTITY_EXCEEDS_POSITION",
            message=(
                f"Asked to close {quantity} contract(s) but only {held} "
                f"are held. Selling more than is held would open a SHORT "
                f"position, which this endpoint will not do."
            ),
        )

    limit_price = resolve_sell_limit(body.limit_price, settings.option_tick_size)

    # The contract is rebuilt from the POSITION, not from the request, so a
    # mistyped identifier cannot sell something else.
    contract = find_option_contract(
        get_quote_client(),
        trade_client,
        position.underlying,
        position.put_call,
        position.strike,
        position.expiry_date_text,
    )

    quote = build_price_only_quote(limit_price)

    # Read off the position now, so building the response later needs nothing
    # that could still raise.
    underlying = position.underlying
    strike = position.strike
    put_call = position.put_call
    expiry_text = position.expiry_date_text
    identifier = contract.identifier

    def record_submission(order_id, estimate) -> None:
        """Write the audit line the moment the order ID is known.

        Before polling, so the trail records the order even if polling dies.
        """
        record = build_order_record(
            settings=settings,
            contract=contract,
            quote=quote,
            estimate=estimate,
            stage="FINAL",
            order_id=order_id,
            outcome=None,
            legs=None,
        )
        record["submitted"] = True
        record["source"] = "api:/positions/close"
        record["client_host"] = request.client.host if request.client else None
        write_order_record(record)

    # ---- from here the order can reach the broker -------------------------
    #
    # FAILURE AFTER SUBMISSION. Anything raising below this line returns an
    # error for an order that may already be live, and a caller who retries
    # sells the position twice. So the only work left is reading values that
    # are already in hand.

    outcome, estimate = sell_option(
        trade_client=trade_client,
        settings=settings,
        contract=contract,
        quote=quote,
        quantity=quantity,
        # There is no interactive prompt over HTTP, so the library's typed
        # confirmation is answered programmatically -- the same way the buy
        # path does. assert_order_allowed still runs before place_order.
        input_function=lambda _prompt: f"{limit_price * quantity * 100:.2f}",
        on_submitted=record_submission,
    )

    return ClosePositionResponse(
        identifier=identifier,
        underlying=underlying,
        strike=strike,
        option_type=put_call,
        expiry=expiry_text,
        quantity_closed=quantity,
        quantity_remaining=held - quantity,
        limit_price=limit_price,
        limit_price_requested=body.limit_price,
        order_id_text=str(outcome.order_id) if outcome.order_id else None,
        order_status=outcome.status,
        filled_quantity=outcome.filled_quantity,
        average_fill_price=outcome.average_fill_price,
        cash_received=estimate.total_cash,
    )
