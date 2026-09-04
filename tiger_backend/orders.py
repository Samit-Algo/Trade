"""Phases 4 and 5 -- build orders, preview them, and submit them.

build_option_order and simulate_order build and show. buy_option and
sell_option submit, to the paper account only, through one gate that cannot
be routed around.

The preview's most important line is CASH REQUIRED. quantity=1 means one
contract, one contract is 100 shares of exposure, and a mistyped quantity
multiplies the cash by 100 per contract. Everything else is context for it.

**The rule that governs everything after submission: an order ID confirms
SUBMISSION, NOT EXECUTION.** Execution is asynchronous. An order that returns
an ID may still be rejected, expire, be cancelled, or fill only in part.
Nothing in this module reports success on receiving an ID.

And a second rule that follows from it: an order whose status is not FILLED --
including NEW, CANCELLED, EXPIRED and REJECTED -- may still be partially
filled. Reality is the `filled` quantity, not the status string. Every outcome
here is computed from `filled` and `avg_fill_price`, never from what was asked
for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from tigeropen.common.util.contract_utils import option_contract
from tigeropen.common.util.order_utils import (
    limit_order,
    limit_order_with_legs,
    order_leg,
)

from .pricing import CostEstimate, estimate_cost
from .providers import QuoteSnapshot
from .safety import assert_order_allowed
from .throttle import ORDERS_LIMITER, PLACE_ORDER_LIMITER

RULE_WIDTH = 60

#: Paper accounts do not support GTC, and this is the SDK's own default.
DEFAULT_TIME_IN_FORCE = "DAY"

#: How many times to ask the broker what happened before giving up.
DEFAULT_POLL_ATTEMPTS = 12

#: Seconds between polls. get_order allows 120/minute, so this is well
#: inside the limit while still settling most orders within a few seconds.
DEFAULT_POLL_DELAY_SECONDS = 2.0

#: The three outcomes that must be told apart.
NOTHING_FILLED = "NOTHING FILLED"
PARTIALLY_FILLED = "PARTIALLY FILLED"
FULLY_FILLED = "FULLY FILLED"

#: Statuses the broker will not move away from on its own. Reaching one
#: stops polling -- but never stops us reading the filled quantity.
TERMINAL_STATUSES = ("FILLED", "CANCELLED", "EXPIRED", "REJECTED")


def build_option_order(
    settings,
    contract,
    action: str,
    quantity: int,
    limit_price: float,
    time_in_force: str = DEFAULT_TIME_IN_FORCE,
):
    """Construct the SDK order object for one option order.

    Builds and returns. Does not submit, and cannot: no submission call is
    imported here.

    The contract object is rebuilt from the identifier that Phase 3 already
    verified against Tiger. option_contract() is a pure local constructor that
    validates nothing, which is exactly right here -- verification happened
    earlier, and this step is assembly.

    Args:
        settings: Validated configuration, for the account number.
        contract: An OptionContractInfo, already verified.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        limit_price: The limit price to place at.
        time_in_force: Defaults to DAY. Paper accounts do not support GTC.

    Returns:
        The SDK Order object, unsent.
    """
    order_contract = option_contract(
        identifier=contract.identifier,
        multiplier=contract.multiplier,
    )

    # Market orders are deliberately not offered. Option spreads are wide and
    # thin contracts fill badly, so a market order here is a foot-gun.
    order = limit_order(
        account=settings.account,
        contract=order_contract,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )

    # Extended hours are deliberately off. Option liquidity outside regular
    # hours is far worse, and market and stop orders do not support it anyway.
    order.outside_rth = False

    return order


def format_money(value: float | None) -> str:
    """Format a cash amount for the preview.

    Args:
        value: The amount, or None.

    Returns:
        The amount to two decimal places, or "-".
    """
    if value is None:
        return "-"
    return f"${value:,.2f}"


def print_manual_data_banner(quote: QuoteSnapshot, max_age_seconds: int) -> None:
    """Print the loud block warning that prices were typed, not fetched.

    Args:
        quote: The quote in use.
        max_age_seconds: The staleness limit, for context.
    """
    if not quote.is_manual:
        return

    age = quote.age_seconds()
    print("-" * RULE_WIDTH)
    print("!!  PRICES BELOW WERE TYPED BY HAND, NOT FETCHED  !!")
    print(
        f"!!  Entered {quote.captured_at.strftime('%H:%M:%S')} UTC, "
        f"{age:.0f}s ago (limit {max_age_seconds}s)"
    )
    print("!!  Nothing has checked them against the live market.")
    print("-" * RULE_WIDTH)


def print_order_preview(
    contract,
    quote: QuoteSnapshot,
    estimate: CostEstimate,
    underlying_price=None,
    liquidity_threshold: int = 10,
    max_age_seconds: int = 60,
    cash_warning: str | None = None,
) -> None:
    """Print the full simulated-order preview.

    Args:
        contract: The OptionContractInfo.
        quote: The QuoteSnapshot the estimate came from.
        estimate: The CostEstimate.
        underlying_price: An UnderlyingPrice, or None if unavailable.
        liquidity_threshold: Below this, volume or OI is flagged thin.
        max_age_seconds: The staleness limit, shown for context.
        cash_warning: A message from pricing.compare_to_available_cash.
    """
    tag = quote.source_tag

    print("-" * RULE_WIDTH)
    print("SIMULATED ORDER - NOTHING WILL BE SENT")
    print("-" * RULE_WIDTH)
    print_manual_data_banner(quote, max_age_seconds)

    print(f"Contract      : {contract.identifier}")

    if underlying_price is not None:
        print(
            f"Underlying    : {contract.underlying} @ "
            f"${underlying_price.price:,.2f}  ({underlying_price.freshness_label})"
        )
    else:
        print(f"Underlying    : {contract.underlying} @ (price unavailable)")

    print(
        f"Type          : {contract.put_call:<8} "
        f"Strike: ${contract.strike:,.2f}"
    )
    print(
        f"Expiry        : {contract.expiry_date_text}  "
        f"({contract.days_to_expiry} days away)"
    )
    print("-" * RULE_WIDTH)

    print(f"Action        : {estimate.action}")
    print("Order type    : LIMIT")

    spread_text = _describe_spread(quote)
    print(
        f"Limit price   : {format_money(quote.limit_price)} {tag}   {spread_text}"
    )
    print(f"Price basis   : {estimate.price_reason}")

    contract_word = "contract" if estimate.quantity == 1 else "contracts"
    print(
        f"Quantity      : {estimate.quantity} {contract_word}  =  "
        f"{estimate.shares_of_exposure:,.0f} shares of exposure"
    )
    print(f"Multiplier    : {estimate.multiplier:,.0f}")
    print("-" * RULE_WIDTH)

    print(f"{estimate.cash_label} : {format_money(estimate.total_cash)}")

    if estimate.break_even_price is not None:
        direction = "exceed" if contract.put_call == "CALL" else "fall below"
        print(
            f"Break-even    : {contract.underlying} must {direction} "
            f"${estimate.break_even_price:,.2f}"
        )

    if estimate.maximum_loss is not None:
        print(
            f"Maximum loss  : {format_money(estimate.maximum_loss)}  "
            f"({estimate.maximum_loss_note})"
        )
    else:
        print(f"Maximum loss  : {estimate.maximum_loss_note}")

    print("-" * RULE_WIDTH)
    print(f"Liquidity     : {_describe_liquidity(quote, liquidity_threshold)} {tag}")
    print("-" * RULE_WIDTH)
    _print_iv_reminder()

    if cash_warning:
        print("NOT ENOUGH CASH")
        print(f"  {cash_warning}")
        print("-" * RULE_WIDTH)


def _describe_spread(quote: QuoteSnapshot) -> str:
    """Describe the quoted market beside the limit price."""
    from .market import calculate_spread

    _spread, spread_percent = calculate_spread(quote.bid, quote.ask)
    if spread_percent is None:
        return f"(ask {quote.ask:,.2f}, bid {quote.bid:,.2f})"
    return (
        f"(ask {quote.ask:,.2f}, bid {quote.bid:,.2f}, "
        f"spread {spread_percent:.1f}%)"
    )


def _describe_liquidity(quote: QuoteSnapshot, threshold: int) -> str:
    """Describe volume and open interest, flagging thin contracts."""
    from .market import is_low_liquidity

    volume_text = f"{quote.volume:,}" if quote.volume is not None else "-"
    open_interest_text = (
        f"{quote.open_interest:,}" if quote.open_interest is not None else "-"
    )

    thin = is_low_liquidity(quote.volume, quote.open_interest, threshold)
    flag = "[THIN]" if thin else "[OK]"

    return f"volume {volume_text} | open interest {open_interest_text}   {flag}"


def _print_iv_reminder() -> None:
    """Print the fixed reminder about implied volatility and earnings.

    Implied volatility is not captured -- it is not used to cost anything, and
    would be a fifth number to transcribe every time. This is a prompt to the
    human, and nothing in the system reads it.
    """
    print("IV / EARNINGS : not captured. Check the app before sending:")
    print("                - Is implied volatility unusually high for this")
    print("                  contract? You may be buying expensive time value")
    print("                  that collapses after the event, even if the")
    print("                  direction is right.")
    print("                - Are earnings due before expiry?")
    print("-" * RULE_WIDTH)


def simulate_order(
    settings,
    contract,
    quote: QuoteSnapshot,
    action: str,
    quantity: int,
    underlying_price=None,
    cash_available: float | None = None,
    liquidity_threshold: int = 10,
    max_age_seconds: int = 60,
) -> tuple[CostEstimate, object]:
    """Build the order, estimate its cost, and print the preview.

    Sends nothing. The order object is returned so a caller can inspect it.

    Args:
        settings: Validated configuration.
        contract: The verified OptionContractInfo.
        quote: The QuoteSnapshot from a MarketDataProvider.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        underlying_price: An UnderlyingPrice, or None.
        cash_available: Cash available to trade, for the affordability check.
        liquidity_threshold: Below this, volume or OI is flagged thin.
        max_age_seconds: The staleness limit, shown for context.

    Returns:
        A pair of (the estimate, the unsent order object).
    """
    from .pricing import compare_to_available_cash

    estimate = estimate_cost(
        contract=contract,
        action=action,
        quantity=quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    cash_warning = None
    if estimate.is_buy:
        cash_warning = compare_to_available_cash(estimate.total_cash, cash_available)

    order = build_option_order(
        settings=settings,
        contract=contract,
        action=estimate.action,
        quantity=estimate.quantity,
        limit_price=quote.limit_price,
    )

    print_order_preview(
        contract=contract,
        quote=quote,
        estimate=estimate,
        underlying_price=underlying_price,
        liquidity_threshold=liquidity_threshold,
        max_age_seconds=max_age_seconds,
        cash_warning=cash_warning,
    )

    return estimate, order


# ---------------------------------------------------------------------------
# Phase 5 -- submission, and finding out what actually happened
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillOutcome:
    """What an order actually did, as opposed to what was asked of it."""

    order_id: int | None
    status: str
    requested_quantity: int
    filled_quantity: int
    average_fill_price: float | None
    actual_cash: float | None
    outcome: str  # NOTHING_FILLED, PARTIALLY_FILLED or FULLY_FILLED
    poll_attempts: int
    reached_terminal_status: bool
    reason: str  # the broker's own explanation, when it gives one

    @property
    def is_settled(self) -> bool:
        """True when the broker has stopped changing this order."""
        return self.reached_terminal_status

    def describe(self) -> str:
        """Return a one-line summary of what happened."""
        return (
            f"{self.outcome}: {self.filled_quantity} of "
            f"{self.requested_quantity} contract(s)"
        )


class OrderSubmissionError(Exception):
    """An order could not be submitted, or the human declined it."""


def normalise_status(raw_status) -> str:
    """Turn whatever the SDK reports as a status into an uppercase name.

    The status arrives as an OrderStatus enum, its name, or its value, and the
    values are not the names -- OrderStatus.REJECTED is the string 'Inactive'
    and OrderStatus.NEW is 'Initial'. Comparing against the wrong one of those
    silently never matches, so everything is funnelled through here.

    This is for display and for deciding when to stop polling. It is never
    what determines whether anything filled.

    Args:
        raw_status: Whatever the Order object carried.

    Returns:
        An uppercase status name, or "UNKNOWN".
    """
    if raw_status is None:
        return "UNKNOWN"

    name = getattr(raw_status, "name", None)
    if name:
        return str(name).upper()

    text = str(raw_status).strip()

    # Map an enum *value* back to its name, e.g. 'Inactive' -> 'REJECTED'.
    try:
        from tigeropen.common.consts import OrderStatus

        for member in OrderStatus:
            if str(member.value).lower() == text.lower():
                return member.name.upper()
    except Exception:
        pass

    return text.upper().replace(" ", "_")


def is_terminal_status(status: str) -> bool:
    """Decide whether the broker has finished with this order.

    Args:
        status: A normalised status name.

    Returns:
        True when no further change is expected.
    """
    return status in TERMINAL_STATUSES


def classify_fill(requested_quantity: int, filled_quantity: int) -> str:
    """Decide which of the three outcomes happened.

    Deliberately takes only quantities. The status string plays no part: an
    order marked CANCELLED or EXPIRED may still have filled part way before it
    ended, and reporting that as "nothing happened" would be false.

    Args:
        requested_quantity: Contracts asked for.
        filled_quantity: Contracts actually filled.

    Returns:
        NOTHING_FILLED, PARTIALLY_FILLED or FULLY_FILLED.
    """
    if filled_quantity <= 0:
        return NOTHING_FILLED
    if filled_quantity >= requested_quantity:
        return FULLY_FILLED
    return PARTIALLY_FILLED


def calculate_actual_cash(
    average_fill_price: float | None,
    filled_quantity: int,
    multiplier: float,
) -> float | None:
    """Work out what the trade actually cost.

    From the average fill price and the filled quantity, never from the price
    asked for or the quantity requested. A partial fill at a different price is
    the normal case this exists to get right.

    Args:
        average_fill_price: What the fills averaged, per share.
        filled_quantity: Contracts actually filled.
        multiplier: Shares per contract.

    Returns:
        The cash that actually moved, or None when nothing filled.
    """
    if average_fill_price is None or filled_quantity <= 0:
        return None
    return round(average_fill_price * filled_quantity * multiplier, 2)


def _read_number(order, field_name: str, default=None):
    """Read a numeric attribute off an Order, tolerating absence."""
    value = getattr(order, field_name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_order_status(trade_client, order_id: int):
    """Fetch one order's current state from the broker.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The global order ID returned at submission.

    Returns:
        The Order object, or None if the broker returned nothing.
    """
    # get_order and get_orders share one wire method and one 120/min limit.
    ORDERS_LIMITER.wait()
    return trade_client.get_order(id=order_id)


def poll_until_settled(
    trade_client,
    order_id: int,
    requested_quantity: int,
    contract_multiplier: float,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
    announce: bool = True,
) -> FillOutcome:
    """Ask the broker what happened, repeatedly, until it stops changing.

    This exists because an order ID means the order was accepted for
    processing and nothing more. Between submission and settlement it may
    fill, part-fill, be rejected, or expire.

    Polling stops on a terminal status or when the attempts run out. Running
    out is reported honestly as "still working" -- not as failure, and
    certainly not as success.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to watch.
        requested_quantity: Contracts asked for.
        contract_multiplier: Shares per contract.
        poll_attempts: Maximum checks.
        poll_delay_seconds: Seconds between checks.
        announce: Whether to print progress.

    Returns:
        What the order actually did.
    """
    status = "UNKNOWN"
    filled_quantity = 0
    average_fill_price = None
    reason = ""
    attempts_used = 0
    reached_terminal = False

    for attempt in range(1, poll_attempts + 1):
        attempts_used = attempt

        if attempt > 1:
            time.sleep(poll_delay_seconds)

        order = get_order_status(trade_client, order_id)
        if order is None:
            if announce:
                print(f"  poll {attempt}/{poll_attempts}: broker returned nothing yet")
            continue

        status = normalise_status(getattr(order, "status", None))
        filled_quantity = int(_read_number(order, "filled", 0) or 0)
        average_fill_price = _read_number(order, "avg_fill_price", None)
        reason = str(getattr(order, "reason", "") or "")

        if announce:
            print(
                f"  poll {attempt}/{poll_attempts}: status={status} "
                f"filled={filled_quantity}/{requested_quantity}"
            )

        if is_terminal_status(status):
            reached_terminal = True
            break

    outcome = classify_fill(requested_quantity, filled_quantity)
    actual_cash = calculate_actual_cash(
        average_fill_price, filled_quantity, contract_multiplier
    )

    return FillOutcome(
        order_id=order_id,
        status=status,
        requested_quantity=requested_quantity,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        actual_cash=actual_cash,
        outcome=outcome,
        poll_attempts=attempts_used,
        reached_terminal_status=reached_terminal,
        reason=reason,
    )


def cancel_order(
    trade_client,
    order_id: int,
    requested_quantity: int = 0,
    contract_multiplier: float = 100.0,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
) -> FillOutcome:
    """Ask the broker to cancel an order, then find out whether it did.

    Cancellation is asynchronous, exactly like submission. A successful return
    confirms the request was accepted, not that the order is cancelled, so this
    polls afterwards rather than announcing success.

    A cancelled order may still have filled in part before it stopped. The
    outcome returned says how much.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to cancel.
        requested_quantity: The original quantity, for classifying the fill.
        contract_multiplier: Shares per contract, for the cash figure.
        poll_attempts: How many times to check the result.
        poll_delay_seconds: Seconds between checks.

    Returns:
        What the order ended up doing.
    """
    from .throttle import CANCEL_ORDER_LIMITER

    CANCEL_ORDER_LIMITER.wait()
    trade_client.cancel_order(id=order_id)

    print("  Cancel request accepted. That is not the same as cancelled.")
    print("  Asking the broker what actually happened...")

    return poll_until_settled(
        trade_client=trade_client,
        order_id=order_id,
        requested_quantity=requested_quantity,
        contract_multiplier=contract_multiplier,
        poll_attempts=poll_attempts,
        poll_delay_seconds=poll_delay_seconds,
    )


def build_cash_confirmation_phrase(estimate: CostEstimate) -> str:
    """Return the exact text the human must type to approve an order.

    The cash amount, not "yes". Typing the number requires reading it, and
    reading it is the last chance to notice that one contract became ten.

    Args:
        estimate: The cost estimate being confirmed.

    Returns:
        The expected text, e.g. "520.00".
    """
    return f"{estimate.total_cash:.2f}"


def confirm_cash_amount(estimate: CostEstimate, input_function=input) -> bool:
    """Ask the human to approve the order by typing the cash amount.

    Args:
        estimate: The cost estimate being confirmed.
        input_function: How to read a line. Injectable for testing.

    Returns:
        True when the typed amount matched exactly.
    """
    expected = build_cash_confirmation_phrase(estimate)
    verb = "leave your account" if estimate.is_buy else "be credited"

    print("-" * RULE_WIDTH)
    print("  TYPE THE CASH AMOUNT TO CONFIRM")
    print("-" * RULE_WIDTH)
    print(f"  {format_money(estimate.total_cash)} will {verb}.")
    print(
        f"  {estimate.quantity} contract(s) x {estimate.multiplier:,.0f} shares "
        f"x {estimate.price_used:,.2f} per share"
    )
    print("")
    print(f"  Type exactly:  {expected}     (anything else cancels)")
    print("-" * RULE_WIDTH)

    try:
        typed = input_function("  > ").strip().replace(",", "").replace("$", "")
    except (EOFError, KeyboardInterrupt):
        return False

    return typed == expected


def _submit_option_order(
    trade_client,
    settings,
    contract,
    quote: QuoteSnapshot,
    action: str,
    quantity: int,
    underlying_price=None,
    cash_available: float | None = None,
    liquidity_threshold: int = 10,
    max_age_seconds: int = 60,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
    input_function=input,
    on_submitted=None,
) -> tuple[FillOutcome, CostEstimate]:
    """Run the mandatory order sequence, in order, with no path around it.

    buy_option and sell_option are thin wrappers over this. The sequence lives
    in one function on purpose: two copies would eventually differ, and the
    difference would be a missing guard.

        1. contract already resolved by the caller (Phase 3)
        2. quote already obtained and checked (Phase 4)
        3. print the full preview
        4. assert_order_allowed(mode, dry_run)
        5. typed confirmation of the cash amount
        6. build the order
        7. submit
        8. poll for the true outcome

    Args:
        trade_client: A tigeropen TradeClient.
        settings: Validated configuration.
        contract: The verified OptionContractInfo.
        quote: The QuoteSnapshot from a MarketDataProvider.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        underlying_price: An UnderlyingPrice, or None.
        cash_available: Cash available to trade.
        liquidity_threshold: Below this, volume or OI is flagged thin.
        max_age_seconds: The staleness limit, shown for context.
        poll_attempts: Maximum status checks after submission.
        poll_delay_seconds: Seconds between checks.
        input_function: How to read the confirmation. Injectable for testing.
        on_submitted: Called with (order_id, estimate) the moment the ID is
            known, before polling, so the audit trail records the order even
            if polling dies.

    Returns:
        A pair of (what the order actually did, the estimate it was based on).

    Raises:
        LiveTradingBlocked: If the safety guard refuses the order.
        OrderSubmissionError: If the human declines, or submission fails.
    """
    # Step 3 -- the preview. Cost is estimated first because the confirmation
    # is of the cash figure, and the human must see it before typing it.
    estimate = estimate_cost(
        contract=contract,
        action=action,
        quantity=quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    from .pricing import compare_to_available_cash

    cash_warning = None
    if estimate.is_buy:
        cash_warning = compare_to_available_cash(estimate.total_cash, cash_available)

    print_order_preview(
        contract=contract,
        quote=quote,
        estimate=estimate,
        underlying_price=underlying_price,
        liquidity_threshold=liquidity_threshold,
        max_age_seconds=max_age_seconds,
        cash_warning=cash_warning,
    )

    # Step 4 -- the guard. Before the human is asked anything, so a blocked
    # order costs no attention.
    assert_order_allowed(settings.mode, settings.dry_run)

    # Step 5 -- typed confirmation of the cash amount, not "yes".
    if not confirm_cash_amount(estimate, input_function=input_function):
        raise OrderSubmissionError("Cash amount not confirmed. Nothing was submitted.")

    # Step 6 -- build.
    order = build_option_order(
        settings=settings,
        contract=contract,
        action=estimate.action,
        quantity=estimate.quantity,
        limit_price=quote.limit_price,
    )

    # Step 4, again, immediately before the submission call. The first check is
    # the gate; this one guarantees that nothing between the gate and the wire
    # changed the mode or cleared the dry-run flag.
    assert_order_allowed(settings.mode, settings.dry_run)

    # Step 7 -- submit.
    print("-" * RULE_WIDTH)
    print("  SUBMITTING...")
    PLACE_ORDER_LIMITER.wait()
    try:
        order_id = trade_client.place_order(order)
    except Exception as error:
        raise OrderSubmissionError(
            f"The broker refused the order: {type(error).__name__}: {error}"
        ) from error

    if order_id is None:
        order_id = getattr(order, "id", None)

    print(f"  Order ID: {order_id}")
    print("  An order ID confirms SUBMISSION, NOT EXECUTION.")
    print("  Asking the broker what actually happened...")
    print("-" * RULE_WIDTH)

    # The estimate goes with the ID so the audit record can be written now,
    # before polling. If polling then dies, the trail still shows that an
    # order went out -- which is the fact that matters most.
    if on_submitted is not None:
        on_submitted(order_id, estimate)

    # Step 8 -- find out what really happened.
    outcome = poll_until_settled(
        trade_client=trade_client,
        order_id=order_id,
        requested_quantity=estimate.quantity,
        contract_multiplier=estimate.multiplier,
        poll_attempts=poll_attempts,
        poll_delay_seconds=poll_delay_seconds,
    )

    return outcome, estimate


def buy_option(trade_client, settings, contract, quote, quantity, **kwargs):
    """Buy an option. Runs the full mandatory sequence.

    Args:
        trade_client: A tigeropen TradeClient.
        settings: Validated configuration.
        contract: The verified OptionContractInfo.
        quote: The QuoteSnapshot.
        quantity: Number of contracts.
        **kwargs: Passed through to the shared sequence.

    Returns:
        A pair of (what the order actually did, the estimate).
    """
    return _submit_option_order(
        trade_client=trade_client,
        settings=settings,
        contract=contract,
        quote=quote,
        action="BUY",
        quantity=quantity,
        **kwargs,
    )


def sell_option(trade_client, settings, contract, quote, quantity, **kwargs):
    """Sell an option. Runs the full mandatory sequence.

    Args:
        trade_client: A tigeropen TradeClient.
        settings: Validated configuration.
        contract: The verified OptionContractInfo.
        quote: The QuoteSnapshot.
        quantity: Number of contracts.
        **kwargs: Passed through to the shared sequence.

    Returns:
        A pair of (what the order actually did, the estimate).
    """
    return _submit_option_order(
        trade_client=trade_client,
        settings=settings,
        contract=contract,
        quote=quote,
        action="SELL",
        quantity=quantity,
        **kwargs,
    )


def print_fill_outcome(outcome: FillOutcome, estimate: CostEstimate | None = None) -> None:
    """Print what actually happened, distinguishing all three outcomes.

    Args:
        outcome: The result of polling.
        estimate: The original estimate, to compare expectation against reality.
    """
    print("=" * RULE_WIDTH)
    print(f"  {outcome.describe()}")
    print("=" * RULE_WIDTH)
    print(f"  Order ID       : {outcome.order_id}")
    print(f"  Broker status  : {outcome.status}")
    print(
        f"  Filled         : {outcome.filled_quantity} of "
        f"{outcome.requested_quantity}"
    )

    if outcome.average_fill_price is not None:
        print(f"  Avg fill price : {outcome.average_fill_price:,.4f}")
    else:
        print("  Avg fill price : -")

    if outcome.actual_cash is not None:
        print(f"  ACTUAL CASH    : {format_money(outcome.actual_cash)}")
        print("                   (avg fill price x filled x multiplier --")
        print("                    never from the quantity that was requested)")
    else:
        print("  ACTUAL CASH    : $0.00   (nothing filled, so nothing moved)")

    if estimate is not None and outcome.actual_cash is not None:
        difference = outcome.actual_cash - estimate.total_cash
        print(
            f"  vs estimate    : {format_money(estimate.total_cash)} estimated, "
            f"difference {difference:+,.2f}"
        )

    if outcome.reason:
        print(f"  Broker reason  : {outcome.reason}")

    if outcome.outcome == PARTIALLY_FILLED:
        print("")
        print(
            f"  PART OF THIS ORDER FILLED. You hold {outcome.filled_quantity} "
            f"contract(s), not {outcome.requested_quantity}."
        )

    if not outcome.reached_terminal_status:
        print("")
        print("  STILL WORKING. Polling ran out before the order settled.")
        print("  This is not success and not failure: the order is live at the")
        print("  broker and may yet fill. Check it again with:")
        print(f"    python scripts/05_paper_order.py --status {outcome.order_id}")

    print("=" * RULE_WIDTH)


# ---------------------------------------------------------------------------
# Phase 7 -- attached take-profit and stop-loss legs
#
# Legs attach to a PARENT order and activate when it fills. They cannot be
# attached to a position you already hold: to bracket an existing holding you
# must close it and buy again with legs attached.
#
# The important operational fact, established by probing on 2026-09-03:
# preview_order REFUSES attached orders --
#     code=1010 biz param error(OCA/ATTACHED order preview not supported)
# for options and for stocks alike, while previewing a plain option order
# fine. So a bracketed order cannot be validated by the broker before it is
# sent. The local checks below are the only pre-submission check that exists,
# which is why they are louder than they would otherwise need to be.
# ---------------------------------------------------------------------------

# Commission model, fitted to four real paper orders placed 2026-09-03:
#
#   BUY  1 contract  @ 0.2800   commission $3.02
#   SELL 1 contract  @ 0.2800   commission $3.02
#   BUY  3 contracts @ 0.0700   commission $3.09
#   SELL 3 contracts @ 0.0600   commission $3.10
#
# Neither simple model fits. Flat would predict $3.02 for three contracts,
# out by $0.07; per-contract would predict $9.06, out by $5.97. A base fee
# plus a small per-contract component reproduces all four:
#
#   1 contract  -> 2.985 + 0.035     = $3.02
#   3 contracts -> 2.985 + 0.105     = $3.09
#
# UNMODELLED: the 3-contract SELL came back at $3.10, one cent above the
# matching buy. One observation is not enough to model it. It may be a
# proceeds-based regulatory fee, which in the US applies to sales and not to
# purchases, in which case it would scale with the money received rather than
# with contracts. Until a second sale at a different size says otherwise, the
# estimate here runs a cent light on the sell side of a multi-contract trade.
#
# The base dominates: tripling the size added seven cents. Commission is, for
# practical purposes, a fixed toll per order.
COMMISSION_BASE = 2.985
COMMISSION_PER_CONTRACT = 0.035

LEG_PROFIT = "PROFIT"
LEG_LOSS = "LOSS"


class BracketError(Exception):
    """The requested bracket prices do not make sense."""


@dataclass(frozen=True)
class BracketLegs:
    """The take-profit and stop-loss attached to a parent order."""

    take_profit_price: float
    stop_loss_price: float
    leg_time_in_force: str

    @property
    def attach_type(self) -> str:
        """Return the attach_type the SDK will put on the wire.

        Mirrors the SDK's own rule so the audit trail can record what was
        actually sent: one leg sends its own type, two send BRACKETS. See
        tigeropen/trade/request/model.py _parse_leg_param.
        """
        return "BRACKETS"


def estimate_commission_per_order(quantity: int) -> float:
    """Estimate the commission on one order of a given size.

    Args:
        quantity: Contracts.

    Returns:
        Estimated commission in cash.
    """
    return round(COMMISSION_BASE + COMMISSION_PER_CONTRACT * quantity, 2)


def estimate_commission_per_share(quantity: int, multiplier: float) -> float:
    """Spread one order's commission across the shares involved.

    Args:
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        Estimated commission per share.
    """
    shares = multiplier * quantity
    if shares <= 0:
        return 0.0
    return estimate_commission_per_order(quantity) / shares


def estimate_round_trip_commission(quantity: int) -> float:
    """Estimate commission for getting in and back out again.

    Two orders: the entry, and whichever leg closes it. On a cheap contract
    this is the largest single cost in the trade -- a round trip on one
    contract at $0.28 costs 21.6% of the premium before the market moves.

    Takes no multiplier: commission is charged per CONTRACT, not per share, so
    the shares-per-contract figure does not enter into it. That distinction is
    the whole point of the measurement above.

    Args:
        quantity: Contracts.

    Returns:
        Estimated round-trip commission in cash.
    """
    return round(estimate_commission_per_order(quantity) * 2, 2)


def is_take_profit_a_losing_exit(
    take_profit_price: float,
    entry_limit_price: float,
    quantity: int,
    multiplier: float,
) -> bool:
    """Decide whether the take-profit would actually lose money.

    A "take profit" set at or below the entry price plus the commission you
    will pay to get out is not taking a profit. It is closing at a loss with
    an encouraging name on it.

    Args:
        take_profit_price: Where the profit leg would sell.
        entry_limit_price: What the parent would pay.
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        True when the exit loses money.
    """
    commission_per_share = estimate_commission_per_share(quantity, multiplier)
    break_even_exit = entry_limit_price + commission_per_share
    return take_profit_price <= break_even_exit


def validate_bracket_prices(
    entry_limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> None:
    """Check the three prices are in a sane order.

    Args:
        entry_limit_price: What the parent pays.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.

    Raises:
        BracketError: If the prices cannot form a working bracket.
    """
    if take_profit_price <= 0 or stop_loss_price <= 0:
        raise BracketError("Bracket prices must be greater than zero.")

    if stop_loss_price >= entry_limit_price:
        raise BracketError(
            f"Stop loss {stop_loss_price:,.2f} is at or above the entry price "
            f"{entry_limit_price:,.2f}. A stop above your entry would trigger "
            "immediately on any fill."
        )

    if take_profit_price <= entry_limit_price:
        raise BracketError(
            f"Take profit {take_profit_price:,.2f} is at or below the entry "
            f"price {entry_limit_price:,.2f}. That is not a profit target."
        )

    # No separate "inverted bracket" check is needed. The two tests above
    # already force stop_loss < entry < take_profit, so take_profit is
    # necessarily above stop_loss by the time execution reaches here. A third
    # check would be unreachable, and unreachable code implies a case that
    # can happen when it cannot.


def calculate_intended_risk(
    entry_limit_price: float,
    stop_loss_price: float,
    quantity: int,
    multiplier: float,
) -> float:
    """Work out the cash the stop is intended to cap the loss at.

    Intended, not guaranteed. A stop is a trigger, not a promise: the fill can
    be worse, and on a gap there may be no fill at that price at all.

    Args:
        entry_limit_price: What the parent pays.
        stop_loss_price: Where the stop leg sells.
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        The intended loss in cash, including estimated round-trip commission.
    """
    price_risk = (entry_limit_price - stop_loss_price) * multiplier * quantity
    commission = estimate_round_trip_commission(quantity)
    return round(price_risk + commission, 2)


def build_option_order_with_bracket(
    settings,
    contract,
    action: str,
    quantity: int,
    limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str = DEFAULT_TIME_IN_FORCE,
    time_in_force: str = DEFAULT_TIME_IN_FORCE,
):
    """Construct a limit order with take-profit and stop-loss legs attached.

    Builds and returns. Does not submit.

    Both legs go in one list. The SDK encodes that as attach_type='BRACKETS',
    which the documented appendix lists as a valid attach type -- so the "only
    one sub-order" limit described in Tiger's app help does not apply to the
    API.

    Args:
        settings: Validated configuration, for the account number.
        contract: An OptionContractInfo, already verified.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        limit_price: The parent's limit price.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.
        leg_time_in_force: Time in force for the LEGS. Parameterised because
            whether a paper account accepts GTC on a leg is undocumented and
            could not be established without submitting; DAY is the SDK's own
            default and the conservative choice.
        time_in_force: Time in force for the parent. Paper rejects GTC.

    Returns:
        The SDK Order object, unsent, with both legs attached.
    """
    order_contract = option_contract(
        identifier=contract.identifier,
        multiplier=contract.multiplier,
    )

    take_profit_leg = order_leg(
        LEG_PROFIT,
        take_profit_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )
    stop_loss_leg = order_leg(
        LEG_LOSS,
        stop_loss_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )

    order = limit_order_with_legs(
        account=settings.account,
        contract=order_contract,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        order_legs=[take_profit_leg, stop_loss_leg],
        time_in_force=time_in_force,
    )

    # Extended hours off, as everywhere else in this project.
    order.outside_rth = False

    return order


def print_bracket_preview(
    contract,
    quote: QuoteSnapshot,
    estimate: CostEstimate,
    legs: BracketLegs,
) -> None:
    """Print the bracket-specific block beneath the ordinary order preview.

    Args:
        contract: The OptionContractInfo.
        quote: The QuoteSnapshot.
        estimate: The CostEstimate for the parent.
        legs: The bracket prices.
    """
    entry_price = estimate.price_used
    quantity = estimate.quantity
    multiplier = estimate.multiplier

    round_trip_commission = estimate_round_trip_commission(quantity)
    commission_per_share = estimate_commission_per_share(quantity, multiplier)
    intended_risk = calculate_intended_risk(
        entry_price, legs.stop_loss_price, quantity, multiplier
    )

    profit_at_target = round(
        (legs.take_profit_price - entry_price) * multiplier * quantity
        - round_trip_commission,
        2,
    )

    print("-" * RULE_WIDTH)
    print("ATTACHED ORDERS (BRACKET)")
    print("-" * RULE_WIDTH)
    print(
        f"  Entry  LIMIT  : {entry_price:,.2f}   "
        "buys the contract; the legs are dormant until it fills"
    )
    print(
        f"  Take profit   : {legs.take_profit_price:,.2f}   "
        "sells if the price rises to here"
    )
    print(
        f"  Stop loss     : {legs.stop_loss_price:,.2f}   "
        "sells if the price falls to here"
    )
    print(f"  Leg time in force : {legs.leg_time_in_force}")
    print(f"  attach_type sent  : {legs.attach_type}")
    print("-" * RULE_WIDTH)
    print(
        f"  Est. round-trip commission : ${round_trip_commission:,.2f}"
        f"  (${commission_per_share:.4f}/share)"
    )
    print("    ESTIMATE. Fitted to four real orders: $2.985 base plus")
    print("    $0.035 per contract, each way. The base dominates, so a")
    print("    small position pays a large percentage.")
    print("-" * RULE_WIDTH)
    print(f"  If the stop triggers : lose about ${intended_risk:,.2f}")
    print(f"  If the target hits   : make about ${profit_at_target:,.2f}")
    print(
        f"  CASH AT RISK         : {format_money(estimate.total_cash)}"
        "   (the whole premium)"
    )
    print("    The stop is a trigger, not a promise. On a gap the fill can be")
    print("    worse than the stop price, or there may be no fill at all, so")
    print("    the full premium is still what you are risking.")
    print("-" * RULE_WIDTH)

    if is_take_profit_a_losing_exit(
        legs.take_profit_price, entry_price, quantity, multiplier
    ):
        break_even_exit = entry_price + commission_per_share
        print("!" * RULE_WIDTH)
        print("!!  THE TAKE PROFIT IS A LOSING EXIT")
        print("!" * RULE_WIDTH)
        print(
            f"!!  Selling at {legs.take_profit_price:,.2f} after paying "
            f"{entry_price:,.2f} does not"
        )
        print("!!  cover the commission to get back out.")
        print(
            f"!!  You need at least {break_even_exit:,.4f} to break even "
            "(estimated)."
        )
        print("!!  A 'take profit' below break-even takes a loss.")
        print("!" * RULE_WIDTH)

    print("  NOT VALIDATED BY THE BROKER.")
    print("    Tiger refuses to preview attached orders:")
    print("    code=1010 'OCA/ATTACHED order preview not supported'.")
    print("    A plain order can be checked before sending; this cannot.")
    print("    The checks above are the only pre-submission check there is.")
    print("-" * RULE_WIDTH)


def buy_option_with_bracket(
    trade_client,
    settings,
    contract,
    quote: QuoteSnapshot,
    quantity: int,
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str = DEFAULT_TIME_IN_FORCE,
    underlying_price=None,
    cash_available: float | None = None,
    liquidity_threshold: int = 10,
    max_age_seconds: int = 60,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
    input_function=input,
    on_submitted=None,
):
    """Buy an option with take-profit and stop-loss legs attached.

    Runs the same mandatory sequence as Phase 5, with the bracket preview and
    checks inserted into step 3. There is no path around the locks: this
    function calls assert_order_allowed twice, exactly as _submit_option_order
    does, and for the same reasons.

        1. contract already resolved by the caller (Phase 3)
        2. quote already obtained and checked (Phase 4)
        3. print the full preview, including the bracket block
        4. assert_order_allowed(mode, dry_run)
        5. typed confirmation of the cash amount
        6. build the order, with legs
        7. submit
        8. poll for the true outcome

    Args:
        trade_client: A tigeropen TradeClient.
        settings: Validated configuration.
        contract: The verified OptionContractInfo.
        quote: The QuoteSnapshot from a MarketDataProvider.
        quantity: Number of contracts.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.
        leg_time_in_force: Time in force for the legs. Default DAY.
        underlying_price: An UnderlyingPrice, or None.
        cash_available: Cash available to trade.
        liquidity_threshold: Below this, volume or OI is flagged thin.
        max_age_seconds: The staleness limit, shown for context.
        poll_attempts: Maximum status checks after submission.
        poll_delay_seconds: Seconds between checks.
        input_function: How to read the confirmation. Injectable for testing.
        on_submitted: Called with (order_id, estimate) before polling.

    Returns:
        A triple of (outcome, estimate, legs).

    Raises:
        BracketError: If the three prices cannot form a working bracket.
        LiveTradingBlocked: If the safety guard refuses the order.
        OrderSubmissionError: If the human declines, or submission fails.
    """
    from .pricing import compare_to_available_cash

    estimate = estimate_cost(
        contract=contract,
        action="BUY",
        quantity=quantity,
        bid=quote.bid,
        ask=quote.ask,
        limit_price=quote.limit_price,
    )

    # Checked before anything is printed, so an impossible bracket fails fast
    # rather than after a page of preview.
    validate_bracket_prices(
        estimate.price_used, take_profit_price, stop_loss_price
    )

    legs = BracketLegs(
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        leg_time_in_force=leg_time_in_force,
    )

    cash_warning = compare_to_available_cash(estimate.total_cash, cash_available)

    # Step 3 -- preview.
    print_order_preview(
        contract=contract,
        quote=quote,
        estimate=estimate,
        underlying_price=underlying_price,
        liquidity_threshold=liquidity_threshold,
        max_age_seconds=max_age_seconds,
        cash_warning=cash_warning,
    )
    print_bracket_preview(contract, quote, estimate, legs)

    # Step 4 -- the guard, before the human is asked anything.
    assert_order_allowed(settings.mode, settings.dry_run)

    # Step 5 -- typed confirmation of the cash amount, not "yes".
    if not confirm_cash_amount(estimate, input_function=input_function):
        raise OrderSubmissionError("Cash amount not confirmed. Nothing was submitted.")

    # Step 6 -- build, with legs.
    order = build_option_order_with_bracket(
        settings=settings,
        contract=contract,
        action="BUY",
        quantity=estimate.quantity,
        limit_price=quote.limit_price,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        leg_time_in_force=leg_time_in_force,
    )

    # Step 4, again, immediately before the wire.
    assert_order_allowed(settings.mode, settings.dry_run)

    # Step 7 -- submit.
    print("-" * RULE_WIDTH)
    print("  SUBMITTING BRACKETED ORDER...")
    print("  This cannot have been validated in advance. If Tiger rejects")
    print("  attached orders on options, this is where it will say so.")
    PLACE_ORDER_LIMITER.wait()
    try:
        order_id = trade_client.place_order(order)
    except Exception as error:
        raise OrderSubmissionError(
            f"The broker refused the bracketed order: "
            f"{type(error).__name__}: {error}"
        ) from error

    if order_id is None:
        order_id = getattr(order, "id", None)

    print(f"  Order ID: {order_id}")
    print("  An order ID confirms SUBMISSION, NOT EXECUTION.")
    print("  Asking the broker what actually happened...")
    print("-" * RULE_WIDTH)

    if on_submitted is not None:
        on_submitted(order_id, estimate)

    # Step 8 -- find out what really happened.
    outcome = poll_until_settled(
        trade_client=trade_client,
        order_id=order_id,
        requested_quantity=estimate.quantity,
        contract_multiplier=estimate.multiplier,
        poll_attempts=poll_attempts,
        poll_delay_seconds=poll_delay_seconds,
    )

    return outcome, estimate, legs


def get_attached_legs(trade_client, parent_order_id: int) -> list:
    """Find the legs attached to a parent order.

    Tried two ways, because which one carries the legs is not documented:
    the parent's own `order_legs` attribute, and any order reporting this one
    as its `parent_id`.

    Args:
        trade_client: A tigeropen TradeClient.
        parent_order_id: The parent order's global ID.

    Returns:
        A list of dicts describing what was found, empty if nothing was.
    """
    from tigeropen.common.consts import Market

    found = []

    parent = get_order_status(trade_client, parent_order_id)
    if parent is not None:
        for leg in getattr(parent, "order_legs", None) or []:
            found.append(
                {
                    "source": "parent.order_legs",
                    "leg_type": getattr(leg, "leg_type", None),
                    "price": getattr(leg, "price", None),
                    "time_in_force": getattr(leg, "time_in_force", None),
                    "outside_rth": getattr(leg, "outside_rth", None),
                }
            )

    ORDERS_LIMITER.wait()
    try:
        recent_orders = trade_client.get_orders(limit=50, market=Market.US)
    except Exception:
        recent_orders = None

    for candidate in recent_orders or []:
        if getattr(candidate, "parent_id", None) == parent_order_id:
            found.append(
                {
                    "source": "child order",
                    "id": getattr(candidate, "id", None),
                    "order_type": getattr(candidate, "order_type", None),
                    "action": getattr(candidate, "action", None),
                    "quantity": getattr(candidate, "quantity", None),
                    "limit_price": getattr(candidate, "limit_price", None),
                    "aux_price": getattr(candidate, "aux_price", None),
                    "time_in_force": getattr(candidate, "time_in_force", None),
                    "status": normalise_status(getattr(candidate, "status", None)),
                }
            )

    return found


def print_attached_legs(legs_found: list, parent_order_id: int) -> None:
    """Print what was found attached to a parent order.

    Args:
        legs_found: The result of get_attached_legs.
        parent_order_id: The parent's ID, for the heading.
    """
    print("=" * RULE_WIDTH)
    print(f"  ATTACHED LEGS ON ORDER {parent_order_id}")
    print("=" * RULE_WIDTH)

    if not legs_found:
        print("  None found.")
        print("")
        print("  That does not by itself prove the legs were rejected. It may")
        print("  mean the legs are not exposed through either route tried")
        print("  (the parent's order_legs, or a child order naming this parent).")
        print("  Check the Tiger app before concluding anything.")
        print("=" * RULE_WIDTH)
        return

    for item in legs_found:
        print(f"  via {item.pop('source')}")
        for key, value in item.items():
            if value is not None:
                print(f"    {key:<16}= {value}")
        print("")

    print("=" * RULE_WIDTH)
