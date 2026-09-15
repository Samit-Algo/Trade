"""Every shape that crosses the HTTP boundary, and how to build one.

Two halves, both about the wire format:

  1. The Pydantic models -- what a request must look like and what a response
     will look like. Field descriptions are written to be read in the
     generated docs at /docs without this file open beside them.
  2. The `shape_*` functions -- turning a library dataclass into one of those
     models. They translate and nothing else; no decisions are made here.

They live together because splitting a shape from its only converter helps
nobody: change one and you always change the other.
"""


from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.service.order import TICK_SOURCE_NOTE
from api.service.order import (
    estimate_commission_per_order,
    estimate_commission_per_share,
    estimate_round_trip_commission,
    normalise_status,
)
from api.service.position import (
    calculate_assignment_exposure,
    calculate_cost_basis,
    is_expiring_soon,
)


# ---------------------------------------------------------------------------
# Read-only responses
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Whether the service is up, and which account it is pointed at."""

    status: Literal["ok"]
    mode: str = Field(description="PAPER or LIVE, resolved by the safety module.")
    dry_run: bool = Field(description="When true, no order can be submitted at all.")
    orders_enabled: bool = Field(
        description="False when DRY_RUN is true or the account is not PAPER. "
        "Order endpoints return 403 in that state."
    )
    market_data_source: str = Field(description="manual or tiger.")
    account_masked: str = Field(description="Last four digits only. The full number is never returned.")


class ExpiryOut(BaseModel):
    """One expiration date, exactly as Tiger listed it."""

    date: str = Field(description="YYYY-MM-DD, as returned. Never constructed locally.")
    days_to_expiry: int = Field(description="Counted on the US/Eastern market clock, not yours.")
    period: str = Field(description="monthly, weekly, or unknown.")
    expired: bool = Field(
        description="True when the date has passed. Tiger keeps returning recently "
        "expired dates, so this is flagged rather than filtered."
    )


class SymbolSettingIn(BaseModel):
    """One symbol's settings, as the page saves them.

    Both percentages are optional and independent. Null means "fall back" --
    to this symbol's .env setting, then to the global default -- so clearing a
    box in the page restores the configured value rather than zeroing it.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="False refuses NEW trades on this symbol, for every "
        "caller. Closing an existing position is unaffected.",
    )
    take_profit: float | None = Field(
        default=None, gt=0, le=1000,
        description="Percent. Null falls back to .env, then the default.",
    )
    stop_loss: float | None = Field(
        default=None, gt=0, lt=100,
        description="Percent. Null falls back to .env, then the default.",
    )


class SymbolSettingOut(SymbolSettingIn):
    """One symbol's settings, with what it actually resolves to."""

    symbol: str
    effective_take_profit: float = Field(
        description="The percentage a trade would really use, after the UI "
        "value, this symbol's .env setting and the default are considered."
    )
    effective_stop_loss: float
    take_profit_source: str = Field(
        description="Which of the three set it, so a surprising bracket is "
        "traceable without reading three files."
    )
    stop_loss_source: str


class SymbolSettingsResponse(BaseModel):
    """Every tradable symbol, and how it is currently configured."""

    symbols: list[SymbolSettingOut]
    default_take_profit: float = Field(
        description="TAKE_PROFIT_PERCENT, used by any symbol with nothing set."
    )
    default_stop_loss: float


class ClosePositionRequest(BaseModel):
    """Which held position to sell, and at what price.

    The price is REQUIRED and there is deliberately no default. This account
    cannot fetch an option bid, so nothing here knows what the contract is
    worth; a market order on a thin option is how a position gets closed at a
    price nobody intended. The caller reads the bid off their screen, or uses
    one of the QUICK_SELL_STEPS the UI offers.
    """

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        min_length=1,
        max_length=32,
        description="The full option identifier, exactly as GET /positions "
        "reports it, e.g. 'NVDA  260914C00215000'.",
    )
    limit_price: float = Field(
        gt=0,
        description="The SELL limit, per share. Snapped DOWN onto a valid "
        "tick -- rounding a sell limit up risks not filling.",
    )
    quantity: int | None = Field(
        default=None,
        gt=0,
        description="Contracts to close. Omit to close the whole position. "
        "More than is held is refused rather than sold short.",
    )


class ClosePositionResponse(BaseModel):
    """What the sell order did, and what is left."""

    identifier: str
    underlying: str
    strike: float
    option_type: str
    expiry: str
    quantity_closed: int
    quantity_remaining: int = Field(
        description="Contracts still held after this order. Non-zero when a "
        "partial close was asked for, or when the order did not fully fill."
    )
    limit_price: float = Field(
        description="The limit actually sent, after snapping onto a tick."
    )
    limit_price_requested: float = Field(
        description="What the caller asked for, before snapping. Differs from "
        "limit_price when the request was not on the tick grid."
    )
    order_id_text: str | None = Field(
        description="A STRING: these exceed 2^53 and a JavaScript client "
        "silently rounds them."
    )
    order_status: str
    filled_quantity: float
    average_fill_price: float | None
    cash_received: float = Field(
        description="What the sale would receive at the limit price. The "
        "actual proceeds follow the fill price, not this."
    )


class SpotPriceResponse(BaseModel):
    """The underlying's share price, and how fresh it is.

    Yahoo, not Tiger: this account has no usStockQuote entitlement, so Tiger's
    own feed runs about 15 minutes behind. See service/market/spot.py.
    """

    symbol: str
    price: float
    age_seconds: float = Field(
        description="Seconds since the exchange stamped this price."
    )
    is_live: bool = Field(
        description="True when this is a currently-trading price rather than "
        "a close being held outside market hours."
    )
    source: str
    note: str = Field(description="One sentence a human can read.")


class ExpirationsResponse(BaseModel):
    """Every expiry Tiger lists for one underlying."""

    underlying: str
    expirations: list[ExpiryOut]


class ContractOut(BaseModel):
    """One verified, tradable contract. Every field came from Tiger."""

    identifier: str = Field(description="The 21-character OCC code.")
    underlying: str
    name: str
    expiry: str = Field(description="YYYY-MM-DD.")
    expiry_compact: str = Field(description="yyyyMMdd, the form Tiger's contract calls want.")
    strike: float
    option_type: str
    multiplier: float = Field(description="Shares per contract. This is what turns $5.20 into $520.")
    contract_id: int | None
    days_to_expiry: int
    min_tick: float | None = Field(
        default=None,
        description="Always null: Tiger does not report it, which is why the limit "
        "price is supplied by the caller rather than snapped to a tick.",
    )


class PositionOut(BaseModel):
    """One option position as held."""

    identifier: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    quantity: float
    multiplier: float
    average_cost: float = Field(
        description="Per share, and INCLUDING commission, as Tiger reports it. "
        "A $28.00 fill comes back as 0.3102 per share, i.e. $31.02 all in."
    )
    cost_basis: float
    days_to_expiry: int
    expiring_soon: bool
    assignment_exposure: float = Field(
        description="Cash an automatic exercise would require: strike x multiplier "
        "x quantity. Unrelated to what the option cost."
    )
    tiger_unrealised_pnl: float | None = Field(
        default=None,
        description="Tiger's own figure, valued at latestPrice. Reported for "
        "comparison only; this API values at the bid.",
    )
    valuation: PositionValuationOut | None = Field(
        default=None,
        description="Null unless a bid was supplied for this contract. Never a "
        "zero standing in for an unknown.",
    )


class PositionValuationOut(BaseModel):
    """A position priced at a supplied bid."""

    current_bid: float = Field(description="A position is worth what someone will PAY for it.")
    source: str = Field(description="MANUAL when the bid was supplied by the caller.")
    current_value: float
    unrealised_pnl: float
    unrealised_pnl_percent: float | None


class PositionsResponse(BaseModel):
    """Every option position held."""

    positions: list[PositionOut]
    expiry_warning_days: int
    total_cost_basis: float
    total_current_value: float | None = Field(
        default=None, description="Null unless every position was valued."
    )
    total_unrealised_pnl: float | None = None


class FillOutcomeOut(BaseModel):
    """What an order actually did, as opposed to what was asked of it."""

    order_id: int | None
    status: str = Field(description="The broker's status. Not what decides whether anything filled.")
    outcome: Literal["NOTHING FILLED", "PARTIALLY FILLED", "FULLY FILLED"] = Field(
        description="Derived from the filled quantity, never from the status string. "
        "A CANCELLED or EXPIRED order may still have filled in part."
    )
    requested_quantity: int
    filled_quantity: int
    average_fill_price: float | None
    actual_cash: float | None = Field(
        description="avg_fill_price x filled x multiplier. Never computed from the "
        "quantity that was requested."
    )
    settled: bool = Field(
        description="False means polling ran out while the order was still working. "
        "That is neither success nor failure; the order is live at the broker."
    )
    poll_attempts: int
    broker_reason: str | None = None


class OrderLegOut(BaseModel):
    """One attached leg, as the broker actually stored it."""

    order_id: int | None
    leg_kind: str = Field(description="TAKE_PROFIT or STOP_LOSS, inferred from the order type.")
    order_type: str | None = Field(description="LMT for a target, STP for a stop.")
    price: float | None = Field(
        description="Read from limit_price for a target and aux_price for a stop. "
        "The two legs use different fields."
    )
    time_in_force: str | None
    status: str | None
    average_fill_price: float | None = Field(
        default=None,
        description="What this leg actually sold at, once it has filled. A "
        "stop becomes a MARKET order when triggered, so its fill can differ "
        "from the trigger price -- that gap is the realised slippage.",
    )
    filled_quantity: int = Field(
        default=0, description="Contracts this leg has filled."
    )


class OrderLegsResponse(BaseModel):
    """The legs attached to a parent order."""

    parent_order_id: int
    legs: list[OrderLegOut]
    note: str = Field(
        description="Explains an empty list, which does not by itself prove the "
        "legs were rejected."
    )


# ---------------------------------------------------------------------------
# The order flow
# ---------------------------------------------------------------------------


class QuoteInput(BaseModel):
    """The five values read off the Tiger app by a human.

    There is no prompt over HTTP, so what the CLI asks for interactively
    arrives here as fields. Every sanity check the CLI runs still runs; a
    failure returns 400 saying which check failed rather than re-prompting.
    """

    bid: float = Field(gt=0, description="Highest price a buyer is currently offering.")
    ask: float = Field(gt=0, description="Lowest price a seller is currently asking.")
    volume: int = Field(ge=0, description="Contracts traded today.")
    limit_price: float = Field(
        gt=0,
        description="The price to place at, at a valid increment as shown in the "
        "app. Supplied rather than computed because Tiger reports no tick size.",
    )


class CommissionOut(BaseModel):
    """Estimated commission, from four measured orders."""

    per_order: float
    round_trip: float
    per_share: float
    basis: str = Field(description="How the estimate was derived, so it is not read as exact.")


PositionOut.model_rebuild()





def shape_contract(contract) -> ContractOut:
    """Convert an OptionContractInfo to its response model."""
    return ContractOut(
        identifier=contract.identifier,
        underlying=contract.underlying,
        name=contract.name,
        expiry=contract.expiry_date_text,
        expiry_compact=contract.expiry_compact,
        strike=contract.strike,
        option_type=contract.put_call,
        multiplier=contract.multiplier,
        contract_id=contract.contract_id,
        days_to_expiry=contract.days_to_expiry,
        min_tick=contract.min_tick,
    )


def shape_commission(quantity: int, multiplier: float) -> CommissionOut:
    """Describe the estimated commission for an order of this size."""
    return CommissionOut(
        per_order=estimate_commission_per_order(quantity),
        round_trip=estimate_round_trip_commission(quantity),
        per_share=round(estimate_commission_per_share(quantity, multiplier), 4),
        basis=(
            "Estimate, fitted to four measured orders: $2.985 base plus $0.035 "
            "per contract, each way. The base dominates, so a small position "
            "pays a large percentage."
        ),
    )


def shape_leg(raw_leg: dict) -> OrderLegOut:
    """Convert one entry from get_attached_legs to its response model.

    The two legs report their price in different fields: a stop is an STP order
    carrying aux_price, a take-profit is an LMT carrying limit_price. Reading
    the wrong one returns None, so both are checked here rather than in a route.
    """
    order_type = raw_leg.get("order_type")

    if order_type == "STP":
        leg_kind = "STOP_LOSS"
        price = raw_leg.get("aux_price")
    elif order_type == "LMT":
        leg_kind = "TAKE_PROFIT"
        price = raw_leg.get("limit_price")
    else:
        leg_kind = "UNKNOWN"
        price = raw_leg.get("limit_price") or raw_leg.get("aux_price")

    status = raw_leg.get("status")

    return OrderLegOut(
        order_id=raw_leg.get("id"),
        leg_kind=leg_kind,
        order_type=order_type,
        price=price,
        time_in_force=raw_leg.get("time_in_force"),
        status=normalise_status(status) if status else None,
        average_fill_price=raw_leg.get("avg_fill_price"),
        filled_quantity=int(raw_leg.get("filled") or 0),
    )


def shape_position(position, threshold_days: int, valuation=None) -> PositionOut:
    """Convert an OptionPosition, optionally with its valuation."""
    valuation_out = None
    if valuation is not None:
        valuation_out = PositionValuationOut(
            current_bid=valuation.current_bid,
            source=valuation.bid_source_tag.strip("[]"),
            current_value=valuation.current_value,
            unrealised_pnl=valuation.unrealised_pnl,
            unrealised_pnl_percent=valuation.unrealised_pnl_percent,
        )

    return PositionOut(
        identifier=position.identifier,
        underlying=position.underlying,
        expiry=position.expiry_date_text,
        strike=position.strike,
        option_type=position.put_call,
        quantity=position.quantity,
        multiplier=position.multiplier,
        average_cost=position.average_cost,
        cost_basis=calculate_cost_basis(
            position.average_cost, position.multiplier, position.quantity
        ),
        days_to_expiry=position.days_to_expiry,
        expiring_soon=is_expiring_soon(position.days_to_expiry, threshold_days),
        assignment_exposure=calculate_assignment_exposure(
            position.strike, position.multiplier, position.quantity
        ),
        tiger_unrealised_pnl=position.tiger_unrealised_pnl,
        valuation=valuation_out,
    )


# ---------------------------------------------------------------------------
# Phase 10 -- the fast single-call trading path
# ---------------------------------------------------------------------------


class TradeRequest(BaseModel):
    """One request, one bracketed BUY. Four inputs, nothing else.

    Quantity, the bracket percentages, the expiry, how far out of the money to
    go and the leg's time in force are all read from .env at startup -- see
    Settings. What stays here is the part that changes trade to trade: which
    contract, and the caller's key for it.

    Whether the order is actually sent is decided by DRY_RUN alone.

    Extra fields are REFUSED. An older caller still sending `quantity` would
    otherwise have it silently ignored and trade the configured size instead,
    which is the worst way for this to go wrong.
    """

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(
        min_length=8,
        max_length=64,
        description="Unique per intended trade. Retrying with the same value "
        "returns the first result instead of placing a second order.",
    )

    symbol: str = Field(min_length=1, max_length=16, description="e.g. AAPL")
    option_type: Literal["CALL", "PUT"]
    current_price: float | None = Field(
        default=None,
        gt=0,
        description="The UNDERLYING's price. Used only to choose the strike -- "
        "it is never used as an option price. OPTIONAL: omit it and the same "
        "live price GET /spot serves is fetched server-side, which costs about "
        "0.4s. Send it when the caller already has a price on screen.",
    )

    # --- optional overrides, FOR TESTING ----------------------------------
    # Absent or null means "use .env". These exist so a different expiry or
    # bracket can be tried without editing .env and restarting.
    #
    # The bounds below are the SAME ones load_settings enforces, so an
    # override cannot reach a value the configured default could not. What is
    # lost by using them is the guarantee that every trade in a session used
    # identical settings -- the response always reports what was actually
    # applied, so check there rather than assuming.
    expiry: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Override TRADE_EXPIRY_DATE for this trade. YYYY-MM-DD.",
    )
    take_profit_percent: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        description="Override TAKE_PROFIT_PERCENT for this trade.",
    )
    stop_loss_percent: float | None = Field(
        default=None,
        gt=0,
        lt=100,
        description="Override STOP_LOSS_PERCENT for this trade.",
    )


class PriceSource(BaseModel):
    """Where the option price came from, so it is never mistaken for a quote."""

    source: Literal["caller", "last_trade"]
    price: float
    age_seconds: float | None = Field(
        default=None,
        description="Seconds since the bar's MINUTE BEGAN -- not since the last "
        "trade. Use is_live for freshness.",
    )
    is_live: bool | None = Field(
        default=None,
        description="True when the contract traded during the current minute. "
        "The real freshness test. Null when the caller supplied the price.",
    )
    recent_volume: int | None = Field(
        default=None, description="Contracts traded in the current minute."
    )
    note: str


class TickDetail(BaseModel):
    """The price grid this order was built on."""

    tick_size: float
    buffer_ticks: int
    buffer_reason: str = Field(
        default="",
        description="Why this many ticks. The buffer can scale with the "
        "premium, so an unexplained limit price would be untraceable.",
    )
    source: str = Field(
        description="How the tick size was arrived at, so it is never mistaken "
        "for something the broker reported."
    )


class BracketPrices(BaseModel):
    """Every number behind the three prices, including the working."""

    entry_price_requested: float = Field(description="What the caller sent.")
    entry_price_snapped: float = Field(description="After snapping to the grid.")
    entry_price_actual: float = Field(description="After the buffer. THE BUY LIMIT.")

    take_profit_percent: float
    take_profit_raw: float = Field(description="Before rounding.")
    take_profit_price: float = Field(description="Rounded UP.")

    stop_loss_percent: float
    stop_loss_raw: float = Field(description="Before rounding.")
    stop_loss_price: float = Field(description="Rounded DOWN.")

    rounding_note: str


class TradeResponse(BaseModel):
    """What the single call did, and every number it decided along the way."""

    order_id: int | None
    duplicate: bool = Field(
        description="True when this replays an earlier identical request. "
        "No second order was placed."
    )
    dry_run: bool = Field(
        description="True when DRY_RUN was on, so every price below was "
        "worked out but nothing was sent to the broker."
    )
    overrides_applied: list[str] = Field(
        default_factory=list,
        description="Which settings the REQUEST overrode for this trade "
        "instead of using .env. Empty means everything came from .env.",
    )

    contract: ContractOut
    symbol: str
    option_type: str
    expiry: str
    strike: float
    quantity: int
    expiry_selection_reason: str
    strike_selection_reason: str
    underlying_price: float = Field(
        description="The underlying price that chose the strike."
    )
    underlying_price_source: str = Field(
        description="Where that price came from -- supplied by the caller, or "
        "fetched server-side when current_price was omitted."
    )
    take_profit_source: str = Field(
        description="Which of the three levels set the take-profit: the "
        "request, this symbol's own .env setting, or the global default."
    )
    stop_loss_source: str = Field(
        description="The same, for the stop loss."
    )

    tick: TickDetail
    price_source: PriceSource
    prices: BracketPrices
    cash_required: float
    commission: CommissionOut

    order_status: str
    parent_filled: int
    legs_submitted: list[OrderLegOut] = Field(
        description="The legs as SENT. Confirming them on the book is a "
        "separate call -- see legs_confirmed."
    )
    legs_confirmed: bool = Field(
        description="Always false here. Legs live as child orders, and reading "
        "them costs a round trip this endpoint deliberately skips."
    )
    legs_note: str
    audit_log: str | None


def shape_tick(calculation, buffer_reason: str = "") -> TickDetail:
    """Describe the price grid an order was built on.

    Args:
        calculation: A BracketCalculation.
        buffer_reason: Why this many buffer ticks, from resolve_buffer_ticks.

    Returns:
        The response model, carrying where the tick size came from.
    """
    return TickDetail(
        tick_size=calculation.tick_size,
        buffer_ticks=calculation.buffer_ticks,
        buffer_reason=buffer_reason,
        source=TICK_SOURCE_NOTE,
    )


def shape_bracket_prices(calculation) -> BracketPrices:
    """Turn a bracket calculation into its response shape.

    The raw, pre-rounding values are included so a reader can tell a
    deliberate rounding from a bug.

    Args:
        calculation: A BracketCalculation.

    Returns:
        The response model.
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


def shape_submitted_legs(calculation, time_in_force: str) -> list[OrderLegOut]:
    """Describe the legs as they were SENT, without asking the broker.

    Confirming them on the book costs a round trip, which POST /trade skips on
    purpose. Every row is marked SUBMITTED rather than given a real status, so
    it cannot be mistaken for a confirmation.

    Args:
        calculation: A BracketCalculation.
        time_in_force: DAY or GTC, as sent on both legs.

    Returns:
        Two rows: the take-profit and the stop-loss.
    """
    return [
        OrderLegOut(
            order_id=None,
            leg_kind="TAKE_PROFIT",
            order_type="LMT",
            price=calculation.take_profit_price,
            time_in_force=time_in_force,
            status="SUBMITTED",
        ),
        OrderLegOut(
            order_id=None,
            leg_kind="STOP_LOSS",
            order_type="STP",
            price=calculation.stop_loss_price,
            time_in_force=time_in_force,
            status="SUBMITTED",
        ),
    ]


class WorkingOrderOut(BaseModel):
    """One order still live on the broker's book for a contract."""

    order_id_text: str = Field(
        description="The id as a STRING. Order ids exceed 2^53, so a "
        "JavaScript client that reads the number gets a different one."
    )
    action: str
    order_type: str | None = Field(description="LMT, STP, or whatever Tiger calls it.")
    price: float | None = Field(description="limit_price for a target, aux_price for a stop.")
    time_in_force: str | None
    status: str | None
    role: Literal["ENTRY", "TAKE_PROFIT", "STOP_LOSS", "OTHER"] = Field(
        description="What this order is for, inferred from its side and type."
    )


class PositionDetailResponse(BaseModel):
    """One held position, priced live, with whatever is protecting it."""

    identifier: str
    underlying: str
    strike: float
    option_type: str
    expiry: str
    days_to_expiry: int
    quantity: float
    multiplier: float

    average_cost: float = Field(description="Per share, and it includes commission.")
    cost_basis: float

    current_price: float | None = Field(
        description="Last traded price from free one-minute bars. NOT a bid."
    )
    price_age_seconds: float | None
    current_value: float | None
    unrealised_pnl: float | None
    unrealised_pnl_percent: float | None

    working_orders: list[WorkingOrderOut]
    has_stop_loss: bool = Field(
        description="True when a live SELL stop is resting on this contract."
    )
    has_take_profit: bool
    protection_note: str

    # --- live timing and distance to the exits ----------------------------
    entry_filled_at: datetime | None = Field(
        default=None,
        description="When the BUY filled -- where the hold clock starts.",
    )
    held_seconds: float | None = Field(
        default=None,
        description="How long this has been held, as of this response.",
    )
    take_profit_price: float | None = Field(
        default=None, description="Where the resting profit leg would sell."
    )
    stop_loss_price: float | None = Field(
        default=None, description="Where the resting stop would sell."
    )
    percent_to_take_profit: float | None = Field(
        default=None,
        description="How much further the price must rise, in percent.",
    )
    percent_to_stop_loss: float | None = Field(
        default=None,
        description="How much cushion is left before the stop, in percent.",
    )


class OrderHistoryRow(BaseModel):
    """One order you placed, and what became of it."""

    order_id_text: str = Field(
        description="A STRING. Order ids exceed 2^53 and JavaScript rounds them."
    )
    placed_at: datetime | None

    identifier: str
    underlying: str
    strike: float | None
    option_type: str | None
    expiry: str | None

    action: str
    quantity: float
    limit_price: float | None
    fill_price: float | None = Field(description="What it actually filled at.")
    status: str

    outcome: Literal[
        "TOOK_PROFIT", "STOPPED_OUT", "CLOSED_MANUALLY", "STILL_OPEN",
        "NOT_FILLED", "CANCELLED", "EXPIRED", "UNKNOWN",
    ]
    outcome_note: str = Field(description="One sentence a human can read.")
    exit_price: float | None = Field(description="What the closing leg filled at.")
    realised_pnl: float | None = Field(
        description="Gross of commission, from the two fill prices."
    )
    realised_pnl_percent: float | None

    take_profit_price: float | None
    stop_loss_price: float | None
    leg_time_in_force: str | None
    legs: list[WorkingOrderOut]

    # --- timing -----------------------------------------------------------
    # Tiger stamps every order twice: order_time when it was accepted, and
    # trade_time when it filled. Both are already stored, so these are
    # computed rather than tracked -- they work for orders placed long
    # before this feature existed.
    filled_at: datetime | None = Field(
        default=None, description="When the BUY actually filled."
    )
    exited_at: datetime | None = Field(
        default=None,
        description="When the closing leg filled. None while still open.",
    )
    fill_delay_seconds: float | None = Field(
        default=None,
        description="Placed to filled. Normally about a second.",
    )
    held_seconds: float | None = Field(
        default=None,
        description="Filled to exited -- how long the position was actually "
        "held. None while still open.",
    )
    held_open_seconds: float | None = Field(
        default=None,
        description="For a position still OPEN: filled to NOW, measured on "
        "the server from the broker's own fill timestamp. The page used to "
        "count from when it first NOTICED the fill, which was wrong by a poll "
        "interval and by however long the position had existed before the tab "
        "was opened.",
    )
    current_price: float | None = Field(
        default=None,
        description="What the contract last traded at, for a position still "
        "open. Cached a few seconds: the page polls every second, and one "
        "broker call per position per second exceeds the documented limit.",
    )
    current_price_age_seconds: float | None = Field(
        default=None,
        description="How long ago the contract last TRADED at that price. "
        "This is not a bid -- on a quiet option it can be a minute old, and "
        "a sell limit set from it may sit unfilled.",
    )
    unrealised_pnl: float | None = Field(
        default=None,
        description="Worth now minus paid, for an open position. Gross of "
        "commission, like realised_pnl.",
    )
    unrealised_pnl_percent: float | None = Field(default=None)


class OrderHistoryResponse(BaseModel):
    """Every order placed on this account, newest first."""

    orders: list[OrderHistoryRow]
    took_profit: int
    stopped_out: int
    closed_manually: int = Field(
        default=0,
        description="Closed by a standalone SELL rather than by either exit "
        "leg -- POST /positions/close, or a sale in the broker app.",
    )
    still_open: int
    total_realised_pnl: float
