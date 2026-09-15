"""POST /trade -- one request, one bracketed BUY.

FOUR INPUTS: client_order_id, symbol, current_price, option_type. Quantity,
the bracket percentages, the expiry, the strike distance and the leg's time in
force all come from .env, validated at startup.

THE FLOW, top to bottom:

    1. Have we seen this request before?   claim_request_id
    2. Work everything out                 prepare_trade      CANNOT SEND
    3. DRY_RUN on?                         describe_only
    4. Send it                             submit_and_record

Read `place_bracketed_trade` at the bottom of this file and you have the whole
endpoint. Everything above it is one of those four steps.

WHY THE SPLIT MATTERS. `prepare_trade` imports nothing that can place an
order, so any failure inside it provably reached no broker -- which is what
makes it safe to hand the caller's id back for reuse. After it returns, the id
is never released again, because a timeout is indistinguishable from a fill.

DRY_RUN IS THE SWITCH. True means every price is worked out and returned with
nothing sent; false means the order goes to the broker. There is no second
per-request flag, so what the .env says is what happens.

THE GUARDS THAT REMAIN, both of which do real work:
  - resolve_account_mode, once at startup, refusing an account that is neither
    the declared paper account nor an explicit live opt-in
  - assert_order_allowed, immediately before place_order inside submit.py

Nothing here places an order itself. It hands three prices to
`buy_option_with_bracket` -- the same function the CLI uses, holding the only
place_order call in the project.

WHAT IS DELIBERATELY NOT DONE, because this endpoint is built for speed:
  no quote fetch        the last traded price is used
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
from api.service.core.live_cache import CACHE
from api.service.core.safety import build_order_record, write_order_record
from api.service.core.symbol_settings import is_enabled, read_for
from api.service.market import (
    fetch_spot_price,
    MAX_RECENT_TRADE_AGE_SECONDS,
    fetch_recent_traded_price,
)
from api.service.order import (
    BracketError,
    TickError,
    buy_option_with_bracket,
    calculate_bracket_from_percentages,
    resolve_buffer_ticks,
    resolve_quantity,
    snap_nearest,
    estimate_cost,
)

from api.service.order.symbol_lock import (
    SymbolBusy,
    claim_symbol,
    release_symbol,
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
    overrides: tuple          # which settings the request overrode, if any
    buffer_reason: str        # why this many buffer ticks
    quantity: int             # contracts, possibly scaled by premium
    quantity_reason: str      # why that many
    underlying_price: float   # what chose the strike
    underlying_price_source: str  # supplied, or fetched
    take_profit_source: str   # request, symbol, or default
    stop_loss_source: str


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


def find_contract(body: TradeRequest, settings, underlying_price: float):
    """Choose and verify the contract, reusing today's answer where possible.

    Args:
        body: The request, for symbol, side and the underlying's price.
        settings: For the expiry, the strike distance and the expiry floor.

    Returns:
        A triple of (contract, why this expiry, why this strike).
    """

    # The caller may override the configured expiry for one trade.
    expiry = body.expiry or settings.trade_expiry_date

    def resolve():
        """Ask Tiger. Called only when the cache misses."""
        return select_contract(
            get_quote_client(),
            get_trade_client(),
            body.symbol,
            body.option_type,
            underlying_price,
            minimum_days=settings.min_days_to_expiry,
            expiry_date_text=expiry,
            strikes_out=settings.trade_strikes_out,
        )

    # current_price picks the strike, so it belongs in the key -- rounded to
    # the nearest dollar, so a two-cent move keeps the cache while a move to
    # the next strike does not.
    key = (
        body.symbol.strip().upper(),
        body.option_type,
        round(underlying_price),
        expiry or "auto",
        settings.trade_strikes_out,
    )
    return get_cached_contract(key, resolve)


def resolve_underlying_price(body: TradeRequest) -> tuple[float, str]:
    """Return the underlying price that will choose the strike, and its source.

    The caller may supply it, or omit it and have it fetched here. Supplying
    it is about 0.4s faster and is the right choice when the caller already
    has a price on screen; omitting it is one fewer thing for a client to get
    wrong, and uses the SAME Yahoo feed GET /spot serves.

    A fetched price is used even when the market is closed -- what comes back
    then is the last trade being held, which is still the right number for
    choosing a strike. The response reports the source either way, so a
    surprising strike can be traced to the price behind it.

    Args:
        body: The validated request.

    Returns:
        The price, and a sentence naming where it came from.

    Raises:
        ApiError: 502 when no price was supplied and none could be fetched.
            Refusing is the only safe answer -- a guessed underlying price
            picks a strike, and there is no sane default for it.
    """
    if body.current_price is not None:
        return body.current_price, "supplied by the caller"

    spot = fetch_spot_price(body.symbol)
    if spot is None:
        raise ApiError(
            status_code=502,
            error_code="SPOT_PRICE_UNAVAILABLE",
            message=(
                f"No current_price was sent and none could be fetched for "
                f"{body.symbol.upper()}. Send current_price, or try again."
            ),
        )

    freshness = (
        f"{spot.age_seconds:.0f}s old"
        if spot.is_live
        else "NOT live -- last trade being held, market is closed"
    )
    return spot.price, f"fetched from {spot.source} ({freshness})"


def resolve_entry_price(contract, settings) -> tuple[float, PriceSource]:
    """Fetch the last price the contract traded at.

    The fetch is FREE -- one-minute bars need no market data entitlement. What
    it returns is a LAST TRADE, not a bid or an ask: nobody is promising to
    sell at it. On a wide spread the real ask sits above it, so a price that
    fetched cleanly can still fail to fill. That is what LIMIT_BUFFER_TICKS is
    for, and 1 tick may not be enough on a thin contract.

    Args:
        contract: The resolved contract, for its identifier.
        settings: For REQUIRE_LIVE_TRADING.

    Returns:
        A pair of (price to trade at, where it came from).

    Raises:
        ApiError: 502 when no price could be fetched, 422 when the newest one
            is too old to trade on.
    """
    recent = fetch_recent_traded_price(get_quote_client(), contract.identifier)

    if recent is None:
        raise ApiError(
            status_code=502,
            error_code="NO_PRICE_AVAILABLE",
            message=(
                f"No traded price could be fetched for {contract.identifier}. "
                "The contract may never have traded."
            ),
        )

    # The real freshness test. age_seconds counts from the bar's MINUTE
    # START, so it climbs to 60 while the price updates every couple of
    # seconds -- it cannot answer "how old is this trade". Whether the
    # contract traded during the current minute can.
    if settings.require_live_trading and not recent.is_live:
        raise ApiError(
            status_code=422,
            error_code="PRICE_NOT_LIVE",
            message=(
                f"{contract.identifier} has not traded during the current "
                "minute, so its price is carried forward from earlier. Pick a "
                "contract with volume, or set REQUIRE_LIVE_TRADING=false."
            ),
            detail={
                "traded_this_minute": recent.traded_this_minute,
                "volume_this_minute": recent.volume,
                "price": recent.price,
            },
        )

    if not recent.is_fresh:
        raise ApiError(
            status_code=422,
            error_code="PRICE_TOO_STALE",
            message=(
                f"The newest trade for {contract.identifier} is "
                f"{recent.age_seconds:,.0f}s old, past the "
                f"{MAX_RECENT_TRADE_AGE_SECONDS}s limit. The market is "
                "probably closed, or this contract is not trading."
            ),
            detail={"age_seconds": recent.age_seconds, "price": recent.price},
        )

    return recent.price, PriceSource(
        source="last_trade",
        price=recent.price,
        age_seconds=recent.age_seconds,
        is_live=recent.is_live,
        recent_volume=recent.volume,
        note=(
            "Last traded price from free one-minute bars. "
            + (
                f"Trading NOW -- {recent.volume} contract(s) this minute."
                if recent.is_live
                else "NOT trading this minute; this price is carried forward."
            )
            + " NOT a bid or ask: no spread data without usOptionQuote."
        ),
    )


def resolve_bracket_percent(
    *, supplied, symbol: str, per_symbol: dict, default: float, name: str
) -> tuple[float, str]:
    """Return the bracket percentage to use, and say where it came from.

    Three levels, highest first:

        1. supplied on the request  -- this trade only
        2. the symbol's own setting -- e.g. TSLA_TAKE_PROFIT_PERCENT
        3. the global default       -- TAKE_PROFIT_PERCENT

    Args:
        supplied: The request's value, or None.
        symbol: The underlying being traded.
        per_symbol: The configured per-symbol mapping.
        default: The global default.
        name: What this is, for the reason string.

    Returns:
        The percentage, and a sentence naming its source.
    """
    if supplied is not None:
        return supplied, f"{supplied:g}% {name}, supplied on the request"

    wanted = symbol.strip().upper()

    # Set in the page, which is editable between trades. .env needs a restart,
    # so this sits above it -- the more recently changed value wins.
    stored = read_for(wanted).get(name.replace(" ", "_"))
    if stored is not None:
        return stored, f"{stored:g}% {name}, set for {wanted} in the UI"

    if wanted in per_symbol:
        value = per_symbol[wanted]
        return value, (
            f"{value:g}% {name}, from {wanted}_"
            f"{name.upper().replace(' ', '_')}_PERCENT"
        )

    return default, f"{default:g}% {name}, the configured default"


def prepare_trade(body: TradeRequest) -> TradePlan:
    """Resolve the contract and work out all three prices.

    THIS FUNCTION CANNOT PLACE AN ORDER. It imports nothing that can, which is
    what makes releasing the caller's id safe when it raises.

    Args:
        body: The validated request.

    Returns:
        Everything the next step needs.

    Raises:
        ApiError: 404 or 422, depending on what was wrong.
    """
    settings = get_settings()

    # The strike is chosen from this, so it is settled before anything else.
    # Supplied by the caller, or fetched here -- see resolve_underlying_price.
    underlying_price, underlying_price_source = resolve_underlying_price(body)

    contract, expiry_reason, strike_reason = find_contract(
        body, settings, underlying_price
    )
    entry_price, price_source = resolve_entry_price(contract, settings)

    # Three levels, highest wins: what the request supplied, then this
    # symbol's own setting, then the global default. The source is carried
    # into the response -- a bracket that is not the one you expected should
    # be traceable to the line of .env that set it.
    take_profit_percent, take_profit_source = resolve_bracket_percent(
        supplied=body.take_profit_percent,
        symbol=contract.underlying,
        per_symbol=settings.symbol_take_profit,
        default=settings.take_profit_percent,
        name="take profit",
    )
    stop_loss_percent, stop_loss_source = resolve_bracket_percent(
        supplied=body.stop_loss_percent,
        symbol=contract.underlying,
        per_symbol=settings.symbol_stop_loss,
        default=settings.stop_loss_percent,
        name="stop loss",
    )

    # OPTIONAL: scale the buy buffer to the premium instead of using a flat
    # LIMIT_BUFFER_TICKS. Resolved against the SNAPPED premium, which is the
    # price the buffer is actually added to. See order/buffer_tiers.py -- with
    # BUFFER_TIERS_ENABLED=false this returns limit_buffer_ticks unchanged.
    buffer_ticks, buffer_reason = resolve_buffer_ticks(
        premium=snap_nearest(entry_price, settings.option_tick_size),
        tick_size=settings.option_tick_size,
        fallback_ticks=settings.limit_buffer_ticks,
        enabled=settings.buffer_tiers_enabled,
        floor_ticks=settings.buffer_tier_floor_ticks,
    )

    try:
        calculation = calculate_bracket_from_percentages(
            entry_price=entry_price,
            take_profit_percent=take_profit_percent,
            stop_loss_percent=stop_loss_percent,
            tick_size=settings.option_tick_size,
            buffer_ticks=buffer_ticks,
        )
    except TickError as error:
        raise ApiError(
            status_code=422, error_code="TICK_INVALID", message=str(error)
        ) from error
    except BracketError as error:
        raise ApiError(
            status_code=422, error_code="BRACKET_INVALID", message=str(error)
        ) from error

    # OPTIONAL: scale the contract count to the premium instead of using a
    # flat TRADE_QUANTITY. Resolved against entry_actual -- the limit actually
    # being paid, buffer included -- so the band reflects real money rather
    # than the pre-buffer quote. See order/quantity_tiers.py; with
    # QUANTITY_TIERS_ENABLED=false this returns trade_quantity unchanged.
    quantity, quantity_reason = resolve_quantity(
        premium=calculation.entry_actual,
        fallback_quantity=settings.trade_quantity,
        enabled=settings.quantity_tiers_enabled,
        tiers=settings.quantity_tiers,
        top_tier_quantity=settings.quantity_tier_top,
    )

    quote = build_price_only_quote(calculation.entry_actual)
    estimate = estimate_cost(
        contract=contract,
        action="BUY",
        quantity=quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    overrides = tuple(
        name
        for name, supplied in (
            ("expiry", body.expiry),
            ("take_profit_percent", body.take_profit_percent),
            ("stop_loss_percent", body.stop_loss_percent),
        )
        if supplied is not None
    )

    return TradePlan(
        contract=contract,
        expiry_reason=expiry_reason,
        strike_reason=strike_reason,
        calculation=calculation,
        quote=quote,
        estimate=estimate,
        price_source=price_source,
        overrides=overrides,
        buffer_reason=buffer_reason,
        quantity=quantity,
        quantity_reason=quantity_reason,
        underlying_price=underlying_price,
        underlying_price_source=underlying_price_source,
        take_profit_source=take_profit_source,
        stop_loss_source=stop_loss_source,
    )


# ---------------------------------------------------------------------------
# Steps 3 and 4 -- answer, or send and answer
# ---------------------------------------------------------------------------


def build_response(
    plan: TradePlan,
    settings,
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
        settings: For the quantity and the dry-run flag.
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
        dry_run=settings.dry_run,
        overrides_applied=list(plan.overrides),
        contract=shape_contract(contract),
        symbol=contract.underlying,
        option_type=contract.put_call,
        expiry=contract.expiry_date_text,
        strike=contract.strike,
        quantity=plan.quantity,
        expiry_selection_reason=plan.expiry_reason,
        strike_selection_reason=plan.strike_reason,
        underlying_price=plan.underlying_price,
        underlying_price_source=plan.underlying_price_source,
        take_profit_source=plan.take_profit_source,
        stop_loss_source=plan.stop_loss_source,
        tick=shape_tick(plan.calculation, plan.buffer_reason),
        price_source=plan.price_source,
        prices=shape_bracket_prices(plan.calculation),
        cash_required=plan.estimate.total_cash,
        commission=shape_commission(
            plan.quantity, plan.estimate.multiplier
        ),
        order_status=order_status,
        parent_filled=parent_filled,
        legs_submitted=legs_submitted,
        legs_confirmed=False,
        legs_note=LEGS_NOTE,
        audit_log=audit_log,
    )


def describe_only(plan: TradePlan, settings) -> TradeResponse:
    """Answer without sending anything. This is what DRY_RUN=true returns.

    Every price is real and was worked out the same way a live trade would
    work it out. Only the submission is skipped.

    Args:
        plan: What was decided.
        settings: For the quantity and the dry-run flag.

    Returns:
        The same shape a real order returns, with no order in it.
    """
    return build_response(
        plan,
        settings,
        order_id=None,
        order_status="NOT_SUBMITTED",
        parent_filled=0,
        legs_submitted=[],
        audit_log=None,
    )


def submit_and_record(
    plan: TradePlan, body: TradeRequest, settings, request: Request
) -> TradeResponse:
    """Send the bracketed order, write the audit record, and report back.

    Past the call below, the broker may have seen the order. Nothing in here
    releases the caller's id.

    Args:
        plan: What was decided.
        body: The original request, for the client_order_id in the audit log.
        settings: The loaded configuration.
        request: For the client address in the audit log.

    Returns:
        What the order actually did.
    """
    log_order_request(
        request, "trade", plan.contract.identifier, plan.estimate.total_cash
    )

    outcome, final_estimate, legs = buy_option_with_bracket(
        trade_client=get_trade_client(),
        settings=settings,
        contract=plan.contract,
        quote=plan.quote,
        quantity=plan.quantity,
        take_profit_price=plan.calculation.take_profit_price,
        stop_loss_price=plan.calculation.stop_loss_price,
        leg_time_in_force=settings.leg_time_in_force,
        # There is no interactive prompt over HTTP, so the library's typed
        # prompt is answered programmatically. assert_order_allowed still runs
        # immediately before place_order; nothing is skipped.
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
        settings,
        order_id=outcome.order_id,
        order_status=outcome.status,
        parent_filled=outcome.filled_quantity,
        legs_submitted=shape_submitted_legs(
            plan.calculation, settings.leg_time_in_force
        ),
        audit_log=log_path.name,
    )


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@router.get("/trade/settings")
def read_trade_settings() -> dict:
    """Report the .env decisions POST /trade will apply, so they are visible.

    A four-field form that places real orders would otherwise hide the numbers
    that matter most -- how many contracts, and where the exits sit. No secret
    is included.

    Returns:
        The trading settings, and whether an order would actually be sent.
    """
    settings = get_settings()
    return {
        "dry_run": settings.dry_run,
        "mode": settings.mode,
        "quantity": settings.trade_quantity,
        "take_profit_percent": settings.take_profit_percent,
        "stop_loss_percent": settings.stop_loss_percent,
        "strikes_out": settings.trade_strikes_out,
        "expiry_date": settings.trade_expiry_date,
        "min_days_to_expiry": settings.min_days_to_expiry,
        "leg_time_in_force": settings.leg_time_in_force,
        "require_live_trading": settings.require_live_trading,
        "limit_buffer_ticks": settings.limit_buffer_ticks,
        "option_tick_size": settings.option_tick_size,
        "max_trade_cash": settings.max_trade_cash,
        "quantity_tiers_enabled": settings.quantity_tiers_enabled,
        "quantity_tiers": [
            {"under_premium": bound, "quantity": quantity}
            for bound, quantity in settings.quantity_tiers
        ],
        "quantity_tier_top": settings.quantity_tier_top,
        "quick_sell_steps": list(settings.quick_sell_steps),
        "trade_symbols": list(settings.trade_symbols),
        "symbol_take_profit": settings.symbol_take_profit,
        "symbol_stop_loss": settings.symbol_stop_loss,
    }


@router.post("/trade", response_model=TradeResponse)
def place_bracketed_trade(body: TradeRequest, request: Request) -> TradeResponse:
    """Resolve, price and place one bracketed BUY in a single request.

    Args:
        body: The four trading inputs, plus the idempotency key.
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

    # 2. Has this symbol been switched off in the page? Checked here rather
    #    than in the UI alone, because "disabled" has to mean disabled for
    #    every caller -- a script or a curl included. Closing is deliberately
    #    NOT gated on this: a symbol you have stopped opening trades on must
    #    still be one you can sell.
    if not is_enabled(body.symbol):
        release_request_id(body.client_order_id)
        raise ApiError(
            status_code=409,
            error_code="SYMBOL_DISABLED",
            message=(
                f"{body.symbol.upper()} is switched off, so no trade was "
                "placed. Enable it in the trade page to trade it again. "
                "Closing an existing position is unaffected."
            ),
        )

    # 3. Is this underlying already busy? One trade per symbol at a time --
    #    an order still on the book counts, not just a filled position, so a
    #    second click during the seconds before a fill is refused too.
    settings = get_settings()
    try:
        claim_symbol(body.symbol, get_trade_client(), settings.account)
    except SymbolBusy as error:
        release_request_id(body.client_order_id)
        raise ApiError(
            status_code=409,
            error_code="SYMBOL_ALREADY_OPEN",
            message=str(error),
        ) from error

    # 3. Work it all out. Nothing here can reach the broker, so a failure is
    #    safe to retry and both the id and the symbol go back.
    try:
        plan = prepare_trade(body)
    except Exception:
        release_symbol(body.symbol)
        release_request_id(body.client_order_id)
        raise

    # 4. Too expensive? MAX_TRADE_CASH is the ceiling on what ONE order may
    #    require. Checked here, after pricing, because the cost is not known
    #    until the contract and limit price are settled -- and checked before
    #    the DRY_RUN branch so a dry run reports the same refusal a live trade
    #    would, rather than describing an order that could never be placed.
    if plan.estimate.total_cash > settings.max_trade_cash:
        release_symbol(body.symbol)
        release_request_id(body.client_order_id)
        raise ApiError(
            status_code=422,
            error_code="TRADE_TOO_EXPENSIVE",
            message=(
                f"This order needs ${plan.estimate.total_cash:,.2f}, which is "
                f"over the ${settings.max_trade_cash:,.2f} MAX_TRADE_CASH "
                f"limit. Nothing was sent. "
                f"{plan.contract.identifier} at "
                f"{plan.calculation.entry_actual:.2f} x "
                f"{plan.quantity} "
                f"({plan.estimate.multiplier:g} shares per contract)."
            ),
        )

    # 5. DRY_RUN on? Every price above is real; only the sending is skipped.
    #    Nothing reached the broker, so the symbol is free again immediately.
    if settings.dry_run:
        release_symbol(body.symbol)
        return remember_and_return(
            body.client_order_id, describe_only(plan, settings)
        )

    # 6. Send it. From here the id is never released: a timeout cannot be
    #    told apart from a fill. The local reservation IS released -- what
    #    keeps the symbol busy from here on is the real order on the book,
    #    which the next request will read.
    try:
        return remember_and_return(
            body.client_order_id, submit_and_record(plan, body, settings, request)
        )
    finally:
        release_symbol(body.symbol)
        # The account just changed. Drop the display cache so the page shows
        # the new order at once rather than the old picture for a few seconds.
        CACHE.invalidate()
