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

from pydantic import BaseModel, Field

from api.service.market import calculate_spread, is_low_liquidity
from api.service.order import TICK_SOURCE_NOTE
from api.service.order import (
    BracketLegs,
    calculate_intended_risk,
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
# Errors
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """The shape of every error this API returns.

    Branch on `error_code`, never on `message`. The message is written for a
    human and will be reworded; the code is stable.
    """

    error_code: str = Field(description="Stable machine-readable code, e.g. EXPIRY_EXPIRED.")
    message: str = Field(description="What went wrong and what to do, in plain English.")
    detail: dict | None = Field(default=None, description="Supporting numbers, when there are any.")


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


class AccountResponse(BaseModel):
    """What Tiger reports about the configured account."""

    account_masked: str
    account_type: str | None = Field(description="PAPER, STANDARD or GLOBAL, as Tiger reports it.")
    status: str | None
    capability: str | None
    currency: str | None
    cash_available_for_trade: float | None = Field(
        description="Cash, deliberately NOT buying power. Buying power on a Reg T "
        "margin account is roughly four times this, and the difference is borrowed."
    )
    buying_power: float | None = Field(description="Shown for completeness. Nothing costs against it.")
    net_liquidation: float | None


class ExpiryOut(BaseModel):
    """One expiration date, exactly as Tiger listed it."""

    date: str = Field(description="YYYY-MM-DD, as returned. Never constructed locally.")
    days_to_expiry: int = Field(description="Counted on the US/Eastern market clock, not yours.")
    period: str = Field(description="monthly, weekly, or unknown.")
    expired: bool = Field(
        description="True when the date has passed. Tiger keeps returning recently "
        "expired dates, so this is flagged rather than filtered."
    )


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
    open_interest: int = Field(ge=0, description="Contracts currently held.")
    limit_price: float = Field(
        gt=0,
        description="The price to place at, at a valid increment as shown in the "
        "app. Supplied rather than computed because Tiger reports no tick size.",
    )


class PreviewRequest(BaseModel):
    """Ask what an order would do. Sends nothing to the broker."""

    underlying: str = Field(description="e.g. AAPL")
    expiry: str = Field(description="YYYY-MM-DD. Must be a date Tiger lists.")
    strike: float
    option_type: Literal["CALL", "PUT"]
    action: Literal["BUY", "SELL"]
    quantity: int = Field(ge=1, description="Contracts. One contract is 100 shares of exposure.")
    quote: QuoteInput

    take_profit_price: float | None = Field(
        default=None, description="Attach a take-profit leg. Requires stop_loss_price."
    )
    stop_loss_price: float | None = Field(
        default=None, description="Attach a stop-loss leg. Requires take_profit_price."
    )
    leg_time_in_force: Literal["DAY", "GTC"] = Field(
        default="DAY",
        description="GTC is confirmed to work on a leg even though a paper account "
        "rejects it on the parent order.",
    )

    confirm_price_override: bool = Field(
        default=False,
        description="Set true ONLY to re-submit a price the decimal-slip check "
        "rejected. It defaults to false and must never be defaulted true: the "
        "whole point is that overriding is a deliberate second act.",
    )


class CommissionOut(BaseModel):
    """Estimated commission, from four measured orders."""

    per_order: float
    round_trip: float
    per_share: float
    basis: str = Field(description="How the estimate was derived, so it is not read as exact.")


class CostOut(BaseModel):
    """What the order would cost, and why."""

    price_used: float
    price_reason: str = Field(description="Ask for a buy, bid for a sell. Never latest_price.")
    quantity: int
    multiplier: float
    shares_of_exposure: float
    total_cash: float
    cash_label: Literal["CASH REQUIRED", "CASH RECEIVED"]
    break_even_price: float | None = Field(
        description="Excludes commission, which on a cheap contract is most of the distance."
    )
    maximum_loss: float | None = Field(
        description="Null when it cannot be bounded, e.g. a short call."
    )
    maximum_loss_note: str


class QuoteOut(BaseModel):
    """The quote a preview was built from, with its provenance."""

    bid: float
    ask: float
    volume: int | None
    open_interest: int | None
    limit_price: float | None
    spread: float | None
    spread_percent: float | None
    source: str = Field(description="MANUAL when typed by a human, TIGER_API when fetched.")
    captured_at: datetime
    last_close: float | None = Field(default=None, description="What the decimal-slip check compared against.")
    last_close_ratio: float | None = None


class LiquidityOut(BaseModel):
    """Whether the contract is thin enough to be hard to get out of."""

    volume: int | None
    open_interest: int | None
    is_thin: bool
    threshold: int


class BracketOut(BaseModel):
    """The attached legs a preview would submit."""

    take_profit_price: float
    stop_loss_price: float
    leg_time_in_force: str
    attach_type: str = Field(description="BRACKETS when both legs are attached.")
    intended_risk: float = Field(
        description="What the stop is intended to cap the loss at, including "
        "estimated round-trip commission. Intended, not guaranteed."
    )
    profit_at_target: float


class PreviewResponse(BaseModel):
    """The full preview, plus the token needed to actually submit it."""

    preview_token: str = Field(
        description="Single-use. Present it to POST /orders together with "
        "expected_cash. The prices are NOT resent at submit time, so a client "
        "cannot preview one price and submit another."
    )
    expires_at: datetime = Field(
        description="Matches the quote staleness limit: a preview built from a "
        "typed quote goes stale for the same reason the quote does."
    )
    expected_cash: float = Field(
        description="Echo this back exactly in POST /orders. It is the HTTP "
        "equivalent of typing the cash amount at the CLI, and it is what stops a "
        "single stray request placing an order."
    )
    contract: ContractOut
    quote: QuoteOut
    cost: CostOut
    liquidity: LiquidityOut
    commission: CommissionOut
    bracket: BracketOut | None = None
    underlying_price: float | None = None
    underlying_price_is_delayed: bool | None = None
    cash_available: float | None = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Things a human should read before submitting: a losing exit, "
        "a wide spread, insufficient cash.",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Context, including that a bracketed order cannot be validated "
        "by the broker before it is sent.",
    )


class SubmitRequest(BaseModel):
    """Actually place the previewed order."""

    preview_token: str = Field(description="From POST /orders/preview. Single use.")
    expected_cash: float = Field(
        description="Must equal the preview's expected_cash exactly. A mismatch is "
        "refused: it means the client is confirming something other than what was "
        "previewed."
    )


class SubmitResponse(BaseModel):
    """What happened after submitting."""

    order_id: int | None
    fill: FillOutcomeOut
    legs: list[OrderLegOut] = Field(default_factory=list)
    audit_log: str = Field(description="Which file the audit record was appended to.")


class CancelResponse(BaseModel):
    """What happened after asking to cancel."""

    order_id: int
    fill: FillOutcomeOut
    note: str = Field(
        description="Cancellation is asynchronous; this reports the state after polling."
    )


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


def shape_quote(quote) -> QuoteOut:
    """Convert a QuoteSnapshot to its response model."""
    spread, spread_percent = calculate_spread(quote.bid, quote.ask)
    return QuoteOut(
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        open_interest=quote.open_interest,
        limit_price=quote.limit_price,
        spread=spread,
        spread_percent=spread_percent,
        source=quote.source.value,
        captured_at=quote.captured_at,
        last_close=quote.last_close,
        last_close_ratio=quote.last_close_ratio,
    )


def shape_cost(estimate) -> CostOut:
    """Convert a CostEstimate to its response model."""
    return CostOut(
        price_used=estimate.price_used,
        price_reason=estimate.price_reason,
        quantity=estimate.quantity,
        multiplier=estimate.multiplier,
        shares_of_exposure=estimate.shares_of_exposure,
        total_cash=estimate.total_cash,
        cash_label=estimate.cash_label,
        break_even_price=estimate.break_even_price,
        maximum_loss=estimate.maximum_loss,
        maximum_loss_note=estimate.maximum_loss_note,
    )


def shape_liquidity(quote, threshold: int) -> LiquidityOut:
    """Describe how thinly traded the contract is."""
    return LiquidityOut(
        volume=quote.volume,
        open_interest=quote.open_interest,
        is_thin=is_low_liquidity(quote.volume, quote.open_interest, threshold),
        threshold=threshold,
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


def shape_bracket(
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str,
    entry_price: float,
    quantity: int,
    multiplier: float,
) -> BracketOut:
    """Describe the attached legs a preview would submit."""
    legs = BracketLegs(take_profit_price, stop_loss_price, leg_time_in_force)
    round_trip = estimate_round_trip_commission(quantity)
    profit_at_target = round(
        (take_profit_price - entry_price) * multiplier * quantity - round_trip, 2
    )

    return BracketOut(
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        leg_time_in_force=leg_time_in_force,
        attach_type=legs.attach_type,
        intended_risk=calculate_intended_risk(
            entry_price, stop_loss_price, quantity, multiplier
        ),
        profit_at_target=profit_at_target,
    )


def shape_fill(outcome) -> FillOutcomeOut:
    """Convert a FillOutcome to its response model."""
    return FillOutcomeOut(
        order_id=outcome.order_id,
        status=outcome.status,
        outcome=outcome.outcome,
        requested_quantity=outcome.requested_quantity,
        filled_quantity=outcome.filled_quantity,
        average_fill_price=outcome.average_fill_price,
        actual_cash=outcome.actual_cash,
        settled=outcome.reached_terminal_status,
        poll_attempts=outcome.poll_attempts,
        broker_reason=outcome.reason or None,
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
    """One request, one bracketed BUY. Phase 10 supports BUY only.

    No quote is sent and none is fetched. `current_price` chooses the strike;
    `entry_price` is the premium the caller is willing to pay. Both come from
    the frontend, which is already looking at them.
    """

    client_order_id: str = Field(
        min_length=8,
        max_length=64,
        description="Unique per intended trade. Retrying with the same value "
        "returns the first result instead of placing a second order.",
    )

    symbol: str = Field(min_length=1, max_length=16, description="e.g. AAPL")
    option_type: Literal["CALL", "PUT"]
    current_price: float = Field(
        gt=0,
        description="The UNDERLYING's price. Used only to choose the strike -- "
        "it is never used as an option price.",
    )
    quantity: int = Field(ge=1, le=1000, description="Contracts. One is 100 shares.")

    expiry: str | None = Field(
        default=None,
        description="Expiry as YYYY-MM-DD. Leave it out and the backend picks "
        "the soonest monthly at least MIN_DAYS_TO_EXPIRY days away.",
    )

    entry_price: float | None = Field(
        default=None,
        gt=0,
        description="The OPTION premium. Leave it out and the backend fetches "
        "the last traded price from free one-minute bars -- seconds old during "
        "the session, but a LAST TRADE, not a bid or an ask.",
    )
    take_profit_percent: float = Field(gt=0, le=1000)
    stop_loss_percent: float = Field(gt=0, lt=100)

    leg_time_in_force: Literal["DAY", "GTC"] = Field(
        default="DAY",
        description="GTC is confirmed to work on a leg, though not on the parent.",
    )
    validate_only: bool = Field(
        default=False,
        description="Run every step and return the prices WITHOUT placing. "
        "Nothing reaches the broker.",
    )


class PriceSource(BaseModel):
    """Where the option price came from, so it is never mistaken for a quote."""

    source: Literal["caller", "last_trade"]
    price: float
    age_seconds: float | None = Field(
        default=None,
        description="How old the fetched price is. Null when the caller sent it.",
    )
    note: str


class TickDetail(BaseModel):
    """The price grid this order was built on."""

    tick_size: float
    buffer_ticks: int
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
    validate_only: bool

    contract: ContractOut
    symbol: str
    option_type: str
    expiry: str
    strike: float
    quantity: int
    expiry_selection_reason: str
    strike_selection_reason: str

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


def shape_tick(calculation) -> TickDetail:
    """Describe the price grid an order was built on.

    Args:
        calculation: A BracketCalculation.

    Returns:
        The response model, carrying where the tick size came from.
    """
    return TickDetail(
        tick_size=calculation.tick_size,
        buffer_ticks=calculation.buffer_ticks,
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
