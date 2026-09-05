"""POST /trade -- one request, one bracketed BUY.

The fast path. Everything the two-step flow does across two requests happens
here in one, and every safety lock still applies:

  - Lock 0, the API key, in the middleware before this file is reached
  - Lock 3 and the mode check, via require_orders_enabled(), 403 up front
  - Locks 1, 2 and 3 again, via assert_order_allowed TWICE inside submit.py

Nothing here places an order. It resolves a contract, computes three prices,
and hands them to `buy_option_with_bracket` -- the same function the CLI and
POST /orders use, containing the only place_order call in the project.

WHAT IS DELIBERATELY NOT DONE, and why:

  no quote fetch        the caller supplies entry_price; the option quote
                        entitlement is not owned anyway
  no underlying fetch   the caller supplies current_price
  no cash check         advisory, and a network round trip
  no decimal-slip check it costs a round trip; `max_cash` replaces it, locally
  no settle polling     one immediate status read, no sleeping

That leaves ONE network call on a warm path: place_order.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.service.contract import (
    find_option_contract,
    list_strikes_for_expiry,
    to_tiger_expiry_format,
    validate_option_type,
)
from api.service.contract.selection import choose_expiry, find_closest_strike
from api.service.core.audit import build_order_record, write_order_record
from api.service.market import list_expirations
from api.service.order import (
    BracketError,
    buy_option_with_bracket,
    calculate_bracket_from_percentages,
    estimate_cost,
)
from api.service.order.ticks import TickError

from ..errors import ApiError
from ..order_rules import RequestInFlight
from ..schemas import (
    BracketPrices,
    TickDetail,
    TradeRequest,
    TradeResponse,
    shape_commission,
    shape_contract,
)
from ..wiring import (
    get_cached_contract,
    get_idempotency_store,
    get_quote_client,
    get_settings,
    get_trade_client,
    log_order_request,
    orders_are_enabled,
)

router = APIRouter(tags=["trade"])

#: How the tick size was arrived at. Carried into every response so nobody
#: mistakes it for something the broker reported -- it is not. Tiger returns
#: min_tick as None on every contract; this number was measured.
TICK_SOURCE = (
    "measured from 32,360 real traded prices across 6 symbols; Tiger reports "
    "no min_tick. See HANDOVER.md section 3d."
)

LEGS_NOTE = (
    "Legs are reported AS SENT. They attach to the parent and activate only "
    "if it fills. Confirm them on the book with GET /orders/{order_id}/legs."
)


def require_orders_enabled() -> None:
    """Refuse before doing any work when a safety lock is closed.

    Raises:
        ApiError: 403 naming the lock.
    """
    settings = get_settings()
    enabled, reason = orders_are_enabled(settings)
    if not enabled:
        raise ApiError(
            status_code=403,
            error_code="BLOCKED_BY_SAFETY_LOCK",
            message=reason,
            detail={"mode": settings.mode, "dry_run": settings.dry_run},
        )


def resolve_contract_for(symbol: str, option_type: str, current_price: float):
    """Turn a symbol, a side and a spot price into one verified contract.

    Three lookups, all free of any market-data entitlement, and all cached for
    the market day: the expiry list, the strike ladder, and the contract
    itself. On a warm cache this makes no network calls at all.

    Args:
        symbol: The underlying, e.g. "AAPL".
        option_type: "CALL" or "PUT".
        current_price: The underlying's price, used ONLY to pick the strike.

    Returns:
        A triple of (contract, why this expiry, why this strike).
    """
    normalised = symbol.strip().upper()
    side = validate_option_type(option_type)

    def build():
        """Resolve from scratch. Called only on a cache miss."""
        quote_client = get_quote_client()
        trade_client = get_trade_client()

        expiries = list_expirations(quote_client, normalised)
        if not expiries:
            raise ApiError(
                status_code=404,
                error_code="SYMBOL_NOT_FOUND",
                message=(
                    f"Tiger lists no option expirations for {normalised!r}. "
                    "Either the symbol is wrong or it has no listed options."
                ),
            )

        expiry, expiry_reason = choose_expiry(
            expiries, minimum_days=get_settings().min_days_to_expiry
        )
        compact = to_tiger_expiry_format(expiry.date_text)

        strikes = list_strikes_for_expiry(trade_client, normalised, compact, side)
        strike, strike_reason = find_closest_strike(strikes, current_price, side)

        # Phase 3, unchanged. This re-checks the expiry and asks Tiger to
        # confirm the contract; the duplicate expiry lookup is the price of
        # not writing a second resolution path, and the cache pays it once.
        contract = find_option_contract(
            quote_client, trade_client, normalised, side, strike, expiry.date_text
        )
        return contract, expiry_reason, strike_reason

    # The strike depends on current_price, which moves, so it is part of the
    # key -- rounded to the nearest dollar, because a two-cent move must not
    # miss the cache while a move to the next strike must.
    key = (normalised, side, round(current_price))
    return get_cached_contract(key, build)


@router.post("/trade", response_model=TradeResponse)
def place_bracketed_trade(body: TradeRequest, request: Request) -> TradeResponse:
    """Resolve, price and place one bracketed BUY in a single request.

    Args:
        body: The seven trading inputs plus the idempotency key and cash cap.
        request: Used for the client address in the audit log.

    Returns:
        The order, and every number behind it.

    Raises:
        ApiError: For any refusal; the code says which.
    """
    settings = get_settings()
    store = get_idempotency_store()

    # Claimed BEFORE anything can reach the broker. A retry that arrives while
    # this is still running is refused rather than risked.
    try:
        replay = store.claim(body.client_order_id)
    except RequestInFlight as error:
        raise ApiError(
            status_code=409, error_code="REQUEST_IN_FLIGHT", message=str(error)
        ) from error

    if replay is not None:
        return TradeResponse(**{**replay, "duplicate": True})

    placed = False
    try:
        if not body.validate_only:
            require_orders_enabled()

        contract, expiry_reason, strike_reason = resolve_contract_for(
            body.symbol, body.option_type, body.current_price
        )

        try:
            calculation = calculate_bracket_from_percentages(
                entry_price=body.entry_price,
                take_profit_percent=body.take_profit_percent,
                stop_loss_percent=body.stop_loss_percent,
                tick_size=settings.option_tick_size,
                buffer_ticks=settings.limit_buffer_ticks,
            )
        except TickError as error:
            raise ApiError(
                status_code=422, error_code="TICK_INVALID", message=str(error)
            ) from error

        quote = build_price_only_snapshot(calculation)
        estimate = estimate_cost(
            contract=contract,
            action="BUY",
            quantity=body.quantity,
            bid=quote.bid,
            ask=quote.ask,
            limit_price=quote.limit_price,
        )

        # The single-call stand-in for the two-step cash confirmation. With no
        # preview to read, this is the only thing that catches a decimal slip
        # in entry_price or a strike far more expensive than intended.
        if estimate.total_cash > body.max_cash:
            raise ApiError(
                status_code=422,
                error_code="MAX_CASH_EXCEEDED",
                message=(
                    f"This order needs {estimate.total_cash:,.2f} but max_cash "
                    f"is {body.max_cash:,.2f}. Nothing was sent. Check "
                    f"entry_price and the resolved strike "
                    f"({contract.strike:,.2f})."
                ),
                detail={
                    "cash_required": estimate.total_cash,
                    "max_cash": body.max_cash,
                    "resolved_strike": contract.strike,
                },
            )

        prices = shape_bracket_prices(calculation)
        tick = TickDetail(
            tick_size=calculation.tick_size,
            buffer_ticks=calculation.buffer_ticks,
            source=TICK_SOURCE,
        )

        common = dict(
            order_id=None,
            duplicate=False,
            validate_only=body.validate_only,
            contract=shape_contract(contract),
            symbol=contract.underlying,
            option_type=contract.put_call,
            expiry=contract.expiry_date_text,
            strike=contract.strike,
            quantity=body.quantity,
            expiry_selection_reason=expiry_reason,
            strike_selection_reason=strike_reason,
            tick=tick,
            prices=prices,
            cash_required=estimate.total_cash,
            max_cash=body.max_cash,
            commission=shape_commission(body.quantity, estimate.multiplier),
            legs_confirmed=False,
            legs_note=LEGS_NOTE,
        )

        if body.validate_only:
            response = TradeResponse(
                **common,
                order_status="NOT_SUBMITTED",
                parent_filled=0,
                legs_submitted=[],
                audit_log=None,
            )
            store.complete(body.client_order_id, response.model_dump(mode="json"))
            return response

        log_order_request(
            request, "trade", contract.identifier, estimate.total_cash
        )

        # From here the broker may have seen the order, so the idempotency key
        # is never released again -- see IdempotencyStore.release.
        placed = True
        outcome, final_estimate, legs = buy_option_with_bracket(
            trade_client=get_trade_client(),
            settings=settings,
            contract=contract,
            quote=quote,
            quantity=body.quantity,
            take_profit_price=calculation.take_profit_price,
            stop_loss_price=calculation.stop_loss_price,
            leg_time_in_force=body.leg_time_in_force,
            # The cash figure was confirmed by max_cash before we got here, so
            # the library's typed prompt is answered programmatically. Both
            # assert_order_allowed calls still run inside; nothing is skipped.
            input_function=lambda _prompt: f"{estimate.total_cash:.2f}",
            # One immediate status read, no sleeping. Settlement is the
            # caller's to watch via GET /orders/{id}.
            poll_attempts=1,
        )

        record = build_order_record(
            settings=settings,
            contract=contract,
            quote=quote,
            estimate=final_estimate,
            stage="FINAL",
            order_id=outcome.order_id,
            outcome=outcome.outcome,
            legs=legs,
        )
        record["submitted"] = True
        record["source"] = "api:/trade"
        record["client_order_id"] = body.client_order_id
        record["client_host"] = request.client.host if request.client else None
        log_path = write_order_record(record)

        response = TradeResponse(
            **{**common, "order_id": outcome.order_id},
            order_status=outcome.status,
            parent_filled=outcome.filled,
            legs_submitted=shape_submitted_legs(calculation, body),
            audit_log=log_path.name,
        )
        store.complete(body.client_order_id, response.model_dump(mode="json"))
        return response

    except BracketError as error:
        if not placed:
            store.release(body.client_order_id)
        raise ApiError(
            status_code=422, error_code="BRACKET_INVALID", message=str(error)
        ) from error
    except Exception:
        # Released ONLY when the broker cannot have seen the order. Once
        # place_order has been attempted the key stays claimed, because a
        # timeout is indistinguishable from a fill.
        if not placed:
            store.release(body.client_order_id)
        raise


def build_price_only_snapshot(calculation):
    """Build the QuoteSnapshot the library expects, from a price and nothing else.

    No bid or ask exists on this path -- no quote was fetched and none was
    sent. Both fields carry the buffered entry so the arithmetic downstream is
    consistent, and the snapshot is stamped MANUAL because that is what it is:
    a price a human read off a screen and a client forwarded.

    volume and open_interest stay None deliberately. is_low_liquidity treats
    missing data as thin, so the absence fails safe rather than reading as
    "perfectly liquid".

    Args:
        calculation: The computed bracket prices.

    Returns:
        A QuoteSnapshot carrying the buffered entry as its limit price.
    """
    from datetime import datetime, timezone

    from api.service.market.quotes import QuoteSnapshot, QuoteSource

    return QuoteSnapshot(
        bid=calculation.entry_actual,
        ask=calculation.entry_actual,
        volume=None,
        open_interest=None,
        limit_price=calculation.entry_actual,
        source=QuoteSource.MANUAL,
        captured_at=datetime.now(timezone.utc),
    )


def shape_bracket_prices(calculation) -> BracketPrices:
    """Turn the calculation into its response shape.

    Args:
        calculation: The BracketCalculation.

    Returns:
        The response model, raw values included so the rounding is auditable.
    """
    return BracketPrices(
        entry_price_requested=calculation.entry_requested,
        entry_price_snapped=calculation.entry_snapped,
        entry_price_actual=calculation.entry_actual,
        take_profit_percent=calculation.take_profit_percent,
        take_profit_raw=calculation.take_profit_raw,
        take_profit_price=calculation.take_profit_price,
        stop_loss_percent=calculation.stop_loss_percent,
        stop_loss_raw=calculation.stop_loss_raw,
        stop_loss_price=calculation.stop_loss_price,
        rounding_note=calculation.rounding_note,
    )


def shape_submitted_legs(calculation, body) -> list:
    """Describe the legs as they were sent, without asking the broker.

    Args:
        calculation: The computed prices.
        body: The request, for quantity and time in force.

    Returns:
        Two OrderLegOut rows, marked as submitted rather than confirmed.
    """
    from ..schemas import OrderLegOut

    return [
        OrderLegOut(
            order_id=None,
            leg_kind="TAKE_PROFIT",
            order_type="LMT",
            price=calculation.take_profit_price,
            time_in_force=body.leg_time_in_force,
            status="SUBMITTED",
        ),
        OrderLegOut(
            order_id=None,
            leg_kind="STOP_LOSS",
            order_type="STP",
            price=calculation.stop_loss_price,
            time_in_force=body.leg_time_in_force,
            status="SUBMITTED",
        ),
    ]
