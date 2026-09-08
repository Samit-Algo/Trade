"""POST /trade -- one request, one bracketed BUY.

THE FLOW, top to bottom:

    1. Have we seen this request before?   claim_request_id
    2. Work everything out                 prepare_trade      CANNOT SEND
    3. Just testing?                       describe_only
    4. Send it                             submit_and_record

Read `place_bracketed_trade` at the bottom of this file and you have the whole
endpoint. Everything above it is one of those four steps.

WHY THE SPLIT MATTERS. `prepare_trade` imports nothing that can place an
order, so any failure inside it provably reached no broker -- which is what
makes it safe to hand the caller's id back for reuse. After it returns, the id
is never released again, because a timeout is indistinguishable from a fill.

EVERY SAFETY LOCK STILL APPLIES:
  - Lock 0, the API key, in the middleware before this file is reached
  - Lock 3 and the mode check, up front, via check_safety_locks
  - Locks 1, 2 and 3 again, via assert_order_allowed TWICE inside submit.py

Nothing here places an order itself. It hands three prices to
`buy_option_with_bracket` -- the same function the CLI uses, holding the only
place_order call in the project.

WHAT IS DELIBERATELY NOT DONE, because this endpoint is built for speed:
  no quote fetch        the caller supplies entry_price
  no underlying fetch   the caller supplies current_price
  no cash check         advisory, and a round trip
  no decimal-slip check a round trip, and it needs a quote to compare to
  no settle polling     one immediate status read, no sleeping

That leaves ONE network call on a warm cache: place_order.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request

from api.service.contract import select_contract
from api.service.core.audit import build_order_record, write_order_record
from api.service.market import (
    MAX_RECENT_TRADE_AGE_SECONDS,
    fetch_recent_traded_price,
)
from api.service.order import (
    BracketError,
    TickError,
    buy_option_with_bracket,
    calculate_bracket_from_percentages,
    estimate_cost,
)

from ..errors import ApiError
from ..order_rules import RequestInFlight, build_price_only_quote
from ..schemas import (
    PriceSource,
    TradeRequest,
    TradeResponse,
    shape_bracket_prices,
    shape_commission,
    shape_contract,
    shape_submitted_legs,
    shape_tick,
)
from ..shared import (
    get_cached_contract,
    get_idempotency_store,
    get_quote_client,
    get_settings,
    get_trade_client,
    log_order_request,
    orders_are_enabled,
)

router = APIRouter(tags=["trade"])

LEGS_NOTE = (
    "Legs are reported AS SENT. They attach to the parent and activate only "
    "if it fills. Confirm them on the book with GET /orders/{order_id}/legs."
)


@dataclass(frozen=True)
class TradePlan:
    """Everything decided before anything was sent.

    Built by prepare_trade, read by everything after it. Holding the decisions
    in one object is what lets the route stay four lines long.
    """

    contract: object          # OptionContractInfo, verified by Tiger
    expiry_reason: str        # why this expiry, for the response
    strike_reason: str        # why this strike
    calculation: object       # BracketCalculation: the three prices
    quote: object             # QuoteSnapshot the library needs
    estimate: object          # CostEstimate: cash required
    price_source: PriceSource # where entry_price came from


# ---------------------------------------------------------------------------
# Step 1 -- has this request been seen before?
# ---------------------------------------------------------------------------


def claim_request_id(client_order_id: str) -> TradeResponse | None:
    """Reserve the caller's id, or hand back what happened to it last time.

    Args:
        client_order_id: The caller's unique key for this trade.

    Returns:
        None when the id is fresh and now reserved, or the original response
        when this exact request already completed.

    Raises:
        ApiError: 409 if an identical request is still running.
    """
    try:
        replay = get_idempotency_store().claim(client_order_id)
    except RequestInFlight as error:
        raise ApiError(
            status_code=409, error_code="REQUEST_IN_FLIGHT", message=str(error)
        ) from error

    if replay is None:
        return None
    return TradeResponse(**{**replay, "duplicate": True})


def release_request_id(client_order_id: str) -> None:
    """Give the id back, so the caller may retry.

    Only ever called after prepare_trade fails, which cannot have sent
    anything. Never after submission.

    Args:
        client_order_id: The id to release.
    """
    get_idempotency_store().release(client_order_id)


def remember_and_return(
    client_order_id: str, response: TradeResponse
) -> TradeResponse:
    """Store the outcome so a retry replays it, and return it.

    Args:
        client_order_id: The id claimed at step 1.
        response: What to replay if the caller asks again.

    Returns:
        The same response.
    """
    get_idempotency_store().complete(
        client_order_id, response.model_dump(mode="json")
    )
    return response


# ---------------------------------------------------------------------------
# Step 2 -- work everything out. NOTHING HERE CAN REACH THE BROKER.
# ---------------------------------------------------------------------------


def check_safety_locks() -> None:
    """Refuse before doing any work when a lock is closed.

    A fast, clear refusal in front of the real guard. assert_order_allowed
    still runs twice inside the library; removing this would change the error
    a caller sees, not whether an order could be placed.

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


def find_contract(body: TradeRequest):
    """Choose and verify the contract, reusing today's answer where possible.

    Args:
        body: The request, for symbol, side and the underlying's price.

    Returns:
        A triple of (contract, why this expiry, why this strike).
    """
    settings = get_settings()

    def resolve():
        """Ask Tiger. Called only when the cache misses."""
        return select_contract(
            get_quote_client(),
            get_trade_client(),
            body.symbol,
            body.option_type,
            body.current_price,
            minimum_days=settings.min_days_to_expiry,
            expiry_date_text=body.expiry,
        )

    # current_price picks the strike, so it belongs in the key -- rounded to
    # the nearest dollar, so a two-cent move keeps the cache while a move to
    # the next strike does not.
    key = (
        body.symbol.strip().upper(),
        body.option_type,
        round(body.current_price),
        body.expiry or "auto",
    )
    return get_cached_contract(key, resolve)


def resolve_entry_price(body: TradeRequest, contract) -> tuple[float, PriceSource]:
    """Use the caller's price, or fetch the last one the contract traded at.

    The fetch is FREE -- one-minute bars need no market data entitlement. What
    it returns is a LAST TRADE, not a bid or an ask: nobody is promising to
    sell at it. On a wide spread the real ask sits above it, so a price that
    fetched cleanly can still fail to fill. That is what LIMIT_BUFFER_TICKS is
    for, and 1 tick may not be enough on a thin contract.

    Args:
        body: The request. `entry_price` may be None.
        contract: The resolved contract, for its identifier.

    Returns:
        A pair of (price to trade at, where it came from).

    Raises:
        ApiError: 502 when no price could be fetched, 422 when the newest one
            is too old to trade on.
    """
    if body.entry_price is not None:
        return body.entry_price, PriceSource(
            source="caller",
            price=body.entry_price,
            age_seconds=None,
            note="Supplied in the request. Nothing was fetched.",
        )

    recent = fetch_recent_traded_price(get_quote_client(), contract.identifier)

    if recent is None:
        raise ApiError(
            status_code=502,
            error_code="NO_PRICE_AVAILABLE",
            message=(
                f"No traded price could be fetched for {contract.identifier}. "
                "The contract may never have traded. Send entry_price yourself."
            ),
        )

    if not recent.is_fresh:
        raise ApiError(
            status_code=422,
            error_code="PRICE_TOO_STALE",
            message=(
                f"The newest trade for {contract.identifier} is "
                f"{recent.age_seconds:,.0f}s old, past the "
                f"{MAX_RECENT_TRADE_AGE_SECONDS}s limit. The market is "
                "probably closed, or this contract is not trading. Send "
                "entry_price yourself to override."
            ),
            detail={"age_seconds": recent.age_seconds, "price": recent.price},
        )

    return recent.price, PriceSource(
        source="last_trade",
        price=recent.price,
        age_seconds=recent.age_seconds,
        note=(
            f"Last traded price, {recent.age_seconds:,.0f}s old, from free "
            "one-minute bars. NOT a bid or ask -- no spread data exists "
            "without the usOptionQuote entitlement."
        ),
    )


def prepare_trade(body: TradeRequest) -> TradePlan:
    """Resolve the contract and work out all three prices.

    THIS FUNCTION CANNOT PLACE AN ORDER. It imports nothing that can, which is
    what makes releasing the caller's id safe when it raises.

    Args:
        body: The validated request.

    Returns:
        Everything the next step needs.

    Raises:
        ApiError: 403, 404 or 422, depending on what was wrong.
    """
    if not body.validate_only:
        check_safety_locks()

    contract, expiry_reason, strike_reason = find_contract(body)
    settings = get_settings()

    entry_price, price_source = resolve_entry_price(body, contract)

    try:
        calculation = calculate_bracket_from_percentages(
            entry_price=entry_price,
            take_profit_percent=body.take_profit_percent,
            stop_loss_percent=body.stop_loss_percent,
            tick_size=settings.option_tick_size,
            buffer_ticks=settings.limit_buffer_ticks,
        )
    except TickError as error:
        raise ApiError(
            status_code=422, error_code="TICK_INVALID", message=str(error)
        ) from error
    except BracketError as error:
        raise ApiError(
            status_code=422, error_code="BRACKET_INVALID", message=str(error)
        ) from error

    quote = build_price_only_quote(calculation.entry_actual)
    estimate = estimate_cost(
        contract=contract,
        action="BUY",
        quantity=body.quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    return TradePlan(
        contract=contract,
        expiry_reason=expiry_reason,
        strike_reason=strike_reason,
        calculation=calculation,
        quote=quote,
        estimate=estimate,
        price_source=price_source,
    )


# ---------------------------------------------------------------------------
# Steps 3 and 4 -- answer, or send and answer
# ---------------------------------------------------------------------------


def build_response(
    plan: TradePlan,
    body: TradeRequest,
    *,
    order_id: int | None,
    order_status: str,
    parent_filled: int,
    legs_submitted: list,
    audit_log: str | None,
) -> TradeResponse:
    """Assemble the response. The only place TradeResponse is built.

    Args:
        plan: What was decided.
        body: The original request.
        order_id: The broker's id, or None when nothing was sent.
        order_status: The status, or NOT_SUBMITTED.
        parent_filled: Contracts filled so far.
        legs_submitted: The legs as sent, or empty.
        audit_log: The audit file written, or None.

    Returns:
        The response.
    """
    contract = plan.contract
    return TradeResponse(
        order_id=order_id,
        duplicate=False,
        validate_only=body.validate_only,
        contract=shape_contract(contract),
        symbol=contract.underlying,
        option_type=contract.put_call,
        expiry=contract.expiry_date_text,
        strike=contract.strike,
        quantity=body.quantity,
        expiry_selection_reason=plan.expiry_reason,
        strike_selection_reason=plan.strike_reason,
        tick=shape_tick(plan.calculation),
        price_source=plan.price_source,
        prices=shape_bracket_prices(plan.calculation),
        cash_required=plan.estimate.total_cash,
        commission=shape_commission(body.quantity, plan.estimate.multiplier),
        order_status=order_status,
        parent_filled=parent_filled,
        legs_submitted=legs_submitted,
        legs_confirmed=False,
        legs_note=LEGS_NOTE,
        audit_log=audit_log,
    )


def describe_only(plan: TradePlan, body: TradeRequest) -> TradeResponse:
    """Answer without sending anything.

    Args:
        plan: What was decided.
        body: The original request.

    Returns:
        The same shape a real order returns, with no order in it.
    """
    return build_response(
        plan,
        body,
        order_id=None,
        order_status="NOT_SUBMITTED",
        parent_filled=0,
        legs_submitted=[],
        audit_log=None,
    )


def submit_and_record(
    plan: TradePlan, body: TradeRequest, request: Request
) -> TradeResponse:
    """Send the bracketed order, write the audit record, and report back.

    Past the call below, the broker may have seen the order. Nothing in here
    releases the caller's id.

    Args:
        plan: What was decided.
        body: The original request.
        request: For the client address in the audit log.

    Returns:
        What the order actually did.
    """
    settings = get_settings()
    log_order_request(
        request, "trade", plan.contract.identifier, plan.estimate.total_cash
    )

    outcome, final_estimate, legs = buy_option_with_bracket(
        trade_client=get_trade_client(),
        settings=settings,
        contract=plan.contract,
        quote=plan.quote,
        quantity=body.quantity,
        take_profit_price=plan.calculation.take_profit_price,
        stop_loss_price=plan.calculation.stop_loss_price,
        leg_time_in_force=body.leg_time_in_force,
        # There is no interactive prompt over HTTP, so the library's
        # typed prompt is answered programmatically. Both assert_order_allowed
        # calls still run inside it; nothing is skipped.
        input_function=lambda _prompt: f"{plan.estimate.total_cash:.2f}",
        # One immediate status read, no sleeping. Watching it settle is the
        # caller's job, via GET /orders/{id}.
        poll_attempts=1,
    )

    record = build_order_record(
        settings=settings,
        contract=plan.contract,
        quote=plan.quote,
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

    return build_response(
        plan,
        body,
        order_id=outcome.order_id,
        order_status=outcome.status,
        parent_filled=outcome.filled,
        legs_submitted=shape_submitted_legs(
            plan.calculation, body.leg_time_in_force
        ),
        audit_log=log_path.name,
    )


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@router.post("/trade", response_model=TradeResponse)
def place_bracketed_trade(body: TradeRequest, request: Request) -> TradeResponse:
    """Resolve, price and place one bracketed BUY in a single request.

    Args:
        body: The seven trading inputs, plus the idempotency key and cash cap.
        request: For the client address in the audit log.

    Returns:
        The order, and every number behind it.

    Raises:
        ApiError: For any refusal; the error_code says which.
    """
    # 1. Seen this request before?
    replay = claim_request_id(body.client_order_id)
    if replay is not None:
        return replay

    # 2. Work it all out. Nothing here can reach the broker, so a failure is
    #    safe to retry and the id goes back.
    try:
        plan = prepare_trade(body)
    except Exception:
        release_request_id(body.client_order_id)
        raise

    # 3. Just testing?
    if body.validate_only:
        return remember_and_return(body.client_order_id, describe_only(plan, body))

    # 4. Send it. From here the id is never released: a timeout cannot be
    #    told apart from a fill.
    return remember_and_return(
        body.client_order_id, submit_and_record(plan, body, request)
    )
