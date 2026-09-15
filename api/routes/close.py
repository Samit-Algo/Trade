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

THE BRACKET LEGS MUST GO FIRST. A bracketed position has its take-profit and
stop-loss resting on the book, and those reserve the whole position: Tiger
answers a sell with "4 shares of your position are reserved for pending
orders. The maximum you can sell is 0", and the order EXPIRES unfilled. So
closing means cancelling the legs, then selling.

That order is deliberate and it has a cost: between the cancel and the fill
the position has no stop. If the sell then fails, the position is left
UNPROTECTED, and the response says so in as many words rather than reporting
a tidy failure. Re-attaching the legs automatically was considered and not
done -- it is a second thing that can fail at the worst moment, and a caller
who is told plainly can act faster than a retry that also breaks.

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
from api.service.order import cancel_order, sell_option, snap_down
from api.service.position import list_option_positions

from ..errors import ApiError
from ..order_rules import build_price_only_quote
from ..schemas import ClosePositionRequest, ClosePositionResponse
from ..shared import get_quote_client, get_settings, get_trade_client
from .positions import fetch_working_orders

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


def cancel_resting_legs(identifier: str, trade_client) -> tuple[list, list]:
    """Cancel the bracket legs holding this position, so it can be sold.

    They reserve the whole position: with a take-profit and a stop-loss on the
    book, Tiger refuses a sell with "the maximum you can sell is 0" and the
    order expires unfilled. Cancelling them is therefore part of closing, not
    an optimisation.

    A leg that will not cancel is NOT fatal here. The sell is attempted anyway
    and will fail on its own if the reservation still stands -- which reports
    the real problem, rather than this guessing at it.

    Args:
        identifier: The full option identifier.
        trade_client: A tigeropen TradeClient.

    Returns:
        A pair of (what was cancelled, what would not cancel), each a list of
        short descriptions for the response.
    """
    cancelled, stubborn = [], []

    for leg in fetch_working_orders(identifier):
        if leg.role not in ("TAKE_PROFIT", "STOP_LOSS"):
            continue

        description = f"{leg.role} {leg.order_type or ''} @ {leg.price}".strip()

        try:
            order_id = int(leg.order_id_text)
        except (TypeError, ValueError):
            stubborn.append(f"{description} (unreadable id)")
            continue

        try:
            cancel_order(trade_client, order_id)
            cancelled.append(description)
        except Exception as error:  # noqa: BLE001 -- the sell reports the truth
            stubborn.append(f"{description} ({error})")

    return cancelled, stubborn


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

    # ---- the legs go first, and the position is exposed from here ---------
    #
    # Cancelling frees the contracts the bracket had reserved. Until the sell
    # fills there is no stop on this position, which is the price of closing
    # it at all -- and it is why a failed sell below says so explicitly.

    cancelled_legs, stubborn_legs = cancel_resting_legs(identifier, trade_client)

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

    # Nothing sold, and the legs are gone: the position is now unprotected.
    # Said plainly, because the caller has to act on it.
    sold = outcome.filled_quantity or 0
    # True whenever contracts are left holding with their legs gone -- a
    # failed sell, a partial fill, or a deliberate partial close.
    unprotected = bool(cancelled_legs) and (held - sold) > 0

    if unprotected and sold <= 0:
        warning = (
            f"NOTHING SOLD, AND THIS POSITION NOW HAS NO STOP LOSS. The "
            f"bracket legs were cancelled to free the contracts "
            f"({'; '.join(cancelled_legs)}), then the sell at "
            f"{limit_price:.2f} came back {outcome.status} without filling. "
            f"{held} contract(s) are still held with nothing protecting them "
            f"-- sell again at a lower limit, or re-place a stop, now."
        )
    elif sold and sold < quantity:
        warning = (
            f"Only {sold:g} of {quantity} sold. {held - sold:g} contract(s) "
            f"are still held with NO STOP LOSS -- the bracket legs were "
            f"cancelled to place this order and cover the whole position, so "
            f"they do not come back for the remainder."
        )
    elif cancelled_legs and (held - sold) > 0:
        # A deliberate partial close. The legs covered the WHOLE position, so
        # cancelling them to free part of it leaves the rest bare. Worth
        # saying: the sell succeeded exactly as asked, and the consequence is
        # still that something is now unprotected.
        warning = (
            f"Sold {sold:g}. The remaining {held - sold:g} contract(s) have "
            f"NO STOP LOSS: the cancelled legs covered all {held}, and "
            f"closing part of the position does not re-place them."
        )
    else:
        warning = None

    if stubborn_legs:
        note = "Some legs would not cancel: " + "; ".join(stubborn_legs)
        warning = f"{warning} {note}" if warning else note

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
        legs_cancelled=cancelled_legs,
        position_unprotected=unprotected,
        warning=warning,
    )
