"""Closing a position: POST /positions/close.

WHY THIS EXISTS. Until this route, the service could open a position and not
close one. The bracket legs WERE the exit -- and they are DAY orders by
default, so the exchange cancels them at the session close and the position is
left with nothing on the book. Discovering that while holding an unprotected
position, with no way to sell through the API, is what this route fixes.

WHAT IT DOES NOT DO. It does not choose a contract, a price, or a size. All
three come from the position being closed, which is the point: closing is not
a trade decision, it is the reversal of one that was already made.

THE SAME GUARD RUNS. sell_option calls assert_order_allowed immediately before
place_order, exactly as the buy path does. A close is still an order.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.service.contract import find_option_contract
from api.service.core.safety import build_order_record, write_order_record
from api.service.order import sell_option
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


@router.post("/positions/close", response_model=ClosePositionResponse)
def close_position(body: ClosePositionRequest, request: Request) -> ClosePositionResponse:
    """Sell a held option position, at a limit price the caller supplies.

    The quantity is the whole position unless a smaller one is given. The
    price is required: this account cannot fetch an option bid, so there is no
    number to default to, and a market order on a thin option is how a
    position gets closed at a price nobody intended.

    Args:
        body: Which position, at what price, and how much of it.
        request: For the client address in the audit log.

    Returns:
        What the sell order actually did.

    Raises:
        ApiError: 404 if not held, 422 if the quantity exceeds the position.
    """
    settings = get_settings()
    trade_client = get_trade_client()

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

    # The contract is rebuilt from the POSITION, not from the request, so a
    # mistyped identifier cannot sell something else. find_option_contract is
    # the entry point that returns a full OptionContractInfo --
    # verify_contract_with_tiger alone returns the SDK's raw Contract, which
    # carries no identifier or underlying.
    contract = find_option_contract(
        get_quote_client(),
        trade_client,
        position.underlying,
        position.put_call,
        position.strike,
        position.expiry_date_text,
    )

    quote = build_price_only_quote(body.limit_price)

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

    outcome, estimate = sell_option(
        trade_client=trade_client,
        settings=settings,
        contract=contract,
        quote=quote,
        quantity=quantity,
        # There is no interactive prompt over HTTP, so the library's typed
        # confirmation is answered programmatically -- the same way the buy
        # path does it. assert_order_allowed still runs before place_order.
        input_function=lambda _prompt: f"{body.limit_price * quantity * 100:.2f}",
        on_submitted=record_submission,
    )

    return ClosePositionResponse(
        identifier=contract.identifier,
        underlying=position.underlying,
        strike=position.strike,
        option_type=position.put_call,
        expiry=position.expiry_date_text,
        quantity_closed=quantity,
        quantity_remaining=held - quantity,
        limit_price=body.limit_price,
        order_id_text=str(outcome.order_id) if outcome.order_id else None,
        order_status=outcome.status,
        filled_quantity=outcome.filled_quantity,
        average_fill_price=outcome.average_fill_price,
        cash_received=estimate.total_cash,
    )

