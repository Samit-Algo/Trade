"""The order flow: preview, submit, inspect, cancel.

The two-step design is the point of this module. `POST /orders/preview`
validates everything and hands back a token; `POST /orders` presents that token
and the exact cash figure. Between them sits the guarantee that a single stray
request cannot place an order.

Handlers stay thin. Every decision -- what price to use, whether the bracket is
sane, what actually filled -- is made by the same library functions the CLI
calls.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.service.core.audit import build_order_record, write_order_record
from api.service.contract import find_option_contract
from api.service.market import DEFAULT_LIQUIDITY_THRESHOLD, fetch_underlying_price_safely
from api.service.order import (
    BracketLegs,
    buy_option,
    buy_option_with_bracket,
    cancel_order,
    get_attached_legs,
    get_order_status,
    is_take_profit_a_losing_exit,
    normalise_status,
    sell_option,
    validate_bracket_prices,
)
from api.service.position import fetch_cash_available
from api.service.order import compare_to_available_cash, estimate_cost

from ..wiring import (
    log_order_request,
    get_quote_client,
    get_settings,
    get_token_store,
    get_trade_client,
    orders_are_enabled,
)
from ..errors import ApiError
from ..schemas import (
    CancelResponse,
    FillOutcomeOut,
    OrderLegsResponse,
    PreviewRequest,
    PreviewResponse,
    SubmitRequest,
    SubmitResponse,
    shape_bracket,
    shape_commission,
    shape_contract,
    shape_cost,
    shape_fill,
    shape_leg,
    shape_liquidity,
    shape_quote,
)
from ..order_rules import (
    TokenExpired,
    TokenNotFound,
    build_quote_snapshot,
    check_for_decimal_slip,
    check_ordinary_rules,
    collect_quote_warnings,
    look_up_last_trade,
)

router = APIRouter(tags=["orders"])

#: The note that goes on every bracketed preview. Tiger refuses to preview
#: attached orders, so unlike a plain order there is no broker-side check
#: before submission -- the local checks are all there is.
BRACKET_NOT_VALIDATED_NOTE = (
    "This order carries attached legs, and Tiger refuses to preview attached "
    "orders (code=1010 'OCA/ATTACHED order preview not supported'). Unlike a "
    "plain order, it cannot be validated by the broker before it is sent. The "
    "checks in this preview are the only pre-submission check that exists."
)


def require_orders_enabled() -> None:
    """Refuse order endpoints when the locks say no.

    A fast, clear refusal in front of the real guard. assert_order_allowed
    still runs twice inside the library on every order path, so removing this
    would change the error a caller sees, not whether an order could be placed.

    Raises:
        ApiError: 403 explaining which lock is closed.
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


@router.post("/orders/preview", response_model=PreviewResponse)
def preview_order(body: PreviewRequest, request: Request) -> PreviewResponse:
    """Cost an order and issue the token needed to submit it. Sends nothing.

    Runs every check the CLI runs. A failed check returns 400 naming it rather
    than re-prompting, because there is nobody to re-prompt.

    Args:
        body: The order intent and the quote read off the Tiger app.
        request: Used only for the client address in the audit log.

    Returns:
        The preview, its token, and the exact cash figure to echo back.
    """
    settings = get_settings()

    # Resolve first: a bad contract should fail before any quote arithmetic.
    contract = find_option_contract(
        get_quote_client(),
        get_trade_client(),
        body.underlying,
        body.option_type,
        body.strike,
        body.expiry,
    )

    check_ordinary_rules(body.quote)

    last_trade = look_up_last_trade(get_quote_client(), contract.identifier)
    check_for_decimal_slip(
        typed_ask=body.quote.ask,
        last_trade=last_trade,
        override_confirmed=body.confirm_price_override,
        multiplier=contract.multiplier,
    )

    quote = build_quote_snapshot(body.quote, last_trade)

    estimate = estimate_cost(
        contract=contract,
        action=body.action,
        quantity=body.quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    wants_bracket = (
        body.take_profit_price is not None or body.stop_loss_price is not None
    )
    if wants_bracket and (
        body.take_profit_price is None or body.stop_loss_price is None
    ):
        raise ApiError(
            status_code=422,
            error_code="BRACKET_INCOMPLETE",
            message="A bracket needs both sides. Send take_profit_price and "
                    "stop_loss_price together, or neither.",
        )

    warnings = collect_quote_warnings(body.quote)
    notes = []

    if last_trade is None:
        notes.append(
            "The decimal-slip check was SKIPPED: this contract has no price "
            "history to compare against. That is not the same as passing it."
        )

    bracket_out = None
    if wants_bracket:
        validate_bracket_prices(
            estimate.price_used, body.take_profit_price, body.stop_loss_price
        )
        bracket_out = shape_bracket(
            body.take_profit_price,
            body.stop_loss_price,
            body.leg_time_in_force,
            estimate.price_used,
            estimate.quantity,
            estimate.multiplier,
        )
        notes.append(BRACKET_NOT_VALIDATED_NOTE)

        if is_take_profit_a_losing_exit(
            body.take_profit_price,
            estimate.price_used,
            estimate.quantity,
            estimate.multiplier,
        ):
            warnings.append(
                f"The take profit at {body.take_profit_price:,.2f} is a LOSING "
                f"exit: after paying {estimate.price_used:,.2f} it does not "
                "cover the estimated commission to get back out. A 'take "
                "profit' below break-even takes a loss."
            )

    cash_available = fetch_cash_available(get_trade_client())
    if estimate.is_buy:
        cash_warning = compare_to_available_cash(estimate.total_cash, cash_available)
        if cash_warning:
            warnings.append(cash_warning)

    underlying_price = fetch_underlying_price_safely(
        get_quote_client(), contract.underlying
    )

    token, intent = get_token_store().issue(
        contract=contract,
        quote=quote,
        estimate=estimate,
        action=estimate.action,
        quantity=estimate.quantity,
        expected_cash=estimate.total_cash,
        take_profit_price=body.take_profit_price,
        stop_loss_price=body.stop_loss_price,
        leg_time_in_force=body.leg_time_in_force,
        underlying_price=underlying_price,
        cash_available=cash_available,
    )

    log_order_request(request, "preview", contract.identifier, estimate.total_cash)

    return PreviewResponse(
        preview_token=token,
        expires_at=intent.expires_at,
        expected_cash=estimate.total_cash,
        contract=shape_contract(contract),
        quote=shape_quote(quote),
        cost=shape_cost(estimate),
        liquidity=shape_liquidity(quote, DEFAULT_LIQUIDITY_THRESHOLD),
        commission=shape_commission(estimate.quantity, estimate.multiplier),
        bracket=bracket_out,
        underlying_price=underlying_price.price if underlying_price else None,
        underlying_price_is_delayed=(
            underlying_price.is_delayed if underlying_price else None
        ),
        cash_available=cash_available,
        warnings=warnings,
        notes=notes,
    )


@router.post("/orders", response_model=SubmitResponse)
def submit_order(body: SubmitRequest, request: Request) -> SubmitResponse:
    """Submit a previewed order. Requires the token AND the exact cash figure.

    Safe to retry. Redeeming a token deletes it, so a client that times out and
    resends gets TOKEN_INVALID rather than a second order on the book.

    The prices are not accepted here. They were fixed at preview time and are
    held server-side, so a client cannot preview one price and submit another.

    Args:
        body: The token and the cash figure to confirm.
        request: Used only for the client address in the audit log.

    Returns:
        What the order actually did, and any attached legs.
    """
    require_orders_enabled()
    settings = get_settings()

    try:
        intent = get_token_store().redeem(body.preview_token)
    except TokenNotFound as error:
        raise ApiError(
            status_code=400, error_code="TOKEN_INVALID", message=str(error)
        ) from error
    except TokenExpired as error:
        raise ApiError(
            status_code=400, error_code="TOKEN_EXPIRED", message=str(error)
        ) from error

    # The HTTP equivalent of typing the cash amount at the CLI. A mismatch
    # means the client is confirming something other than what was previewed,
    # so it is refused rather than reconciled.
    if round(body.expected_cash, 2) != round(intent.expected_cash, 2):
        raise ApiError(
            status_code=400,
            error_code="CASH_MISMATCH",
            message=(
                f"expected_cash was {body.expected_cash:,.2f} but the preview "
                f"said {intent.expected_cash:,.2f}. The token has been consumed; "
                "request a fresh preview and confirm the figure it returns."
            ),
            detail={
                "sent": body.expected_cash,
                "expected": intent.expected_cash,
            },
        )

    log_order_request(
        request, "submit", intent.contract.identifier, intent.expected_cash
    )

    shared = dict(
        trade_client=get_trade_client(),
        settings=settings,
        contract=intent.contract,
        quote=intent.quote,
        quantity=intent.quantity,
        underlying_price=intent.underlying_price,
        cash_available=intent.cash_available,
        # The cash figure was already confirmed by matching expected_cash, so
        # the library's interactive prompt is satisfied programmatically here.
        # assert_order_allowed still runs twice inside; nothing is bypassed.
        input_function=lambda _prompt: f"{intent.expected_cash:.2f}",
    )

    legs_used = None
    if intent.has_bracket:
        outcome, estimate, legs_used = buy_option_with_bracket(
            take_profit_price=intent.take_profit_price,
            stop_loss_price=intent.stop_loss_price,
            leg_time_in_force=intent.leg_time_in_force,
            **shared,
        )
    elif intent.action == "BUY":
        outcome, estimate = buy_option(**shared)
    else:
        outcome, estimate = sell_option(**shared)

    record = build_order_record(
        settings=settings,
        contract=intent.contract,
        quote=intent.quote,
        estimate=estimate,
        stage="FINAL",
        order_id=outcome.order_id,
        outcome=outcome.outcome,
        legs=legs_used,
    )
    record["submitted"] = True
    record["source"] = "api"
    record["client_host"] = request.client.host if request.client else None
    log_path = write_order_record(record)

    leg_rows = []
    if legs_used is not None and outcome.order_id is not None:
        for raw_leg in get_attached_legs(get_trade_client(), outcome.order_id):
            if raw_leg.get("source") == "child order":
                leg_rows.append(shape_leg(raw_leg))

    return SubmitResponse(
        order_id=outcome.order_id,
        fill=shape_fill(outcome),
        legs=leg_rows,
        audit_log=log_path.name,
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


@router.delete("/orders/{order_id}", response_model=CancelResponse)
def cancel_one_order(order_id: int, request: Request) -> CancelResponse:
    """Ask the broker to cancel an order, then report what it actually did.

    Cancellation is asynchronous. A successful return from the broker confirms
    the request was accepted, not that the order is cancelled, so this polls
    afterwards. A cancelled order may still have filled in part before it
    stopped.

    Args:
        order_id: The order to cancel.
        request: Used only for the client address in the audit log.

    Returns:
        What the order ended up doing.
    """
    require_orders_enabled()

    existing = get_order_status(get_trade_client(), order_id)
    requested_quantity = int(getattr(existing, "quantity", 0) or 0) if existing else 0

    log_order_request(request, "cancel", str(order_id), None)

    outcome = cancel_order(
        trade_client=get_trade_client(),
        order_id=order_id,
        requested_quantity=requested_quantity,
    )

    return CancelResponse(
        order_id=order_id,
        fill=shape_fill(outcome),
        note=(
            "Cancellation is asynchronous. This is the state after polling, not "
            "an acknowledgement that the request was accepted."
        ),
    )
