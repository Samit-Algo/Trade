"""Every request and response shape the API accepts or returns.

Field descriptions are written to be read in the generated OpenAPI docs
without this file open beside them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


class CapabilityRow(BaseModel):
    """One endpoint's reachability."""

    category: str
    endpoint: str
    outcome: Literal["OK", "DENIED", "ERROR"]
    detail: str
    entitlement: str | None = Field(default=None, description="Named when the refusal says which one.")


class CapabilitiesResponse(BaseModel):
    """The endpoint probe. DENIED is an expected result, not a failure."""

    captured_at: datetime = Field(description="When this probe actually ran.")
    age_seconds: float = Field(description="How old these results are, right now.")
    age_description: str = Field(
        description="The age in words, so a stale probe is not misread as current."
    )
    is_stale: bool = Field(description="True when the probe is older than an hour.")
    results: list[CapabilityRow]


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
