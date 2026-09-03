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
from tigeropen.common.util.order_utils import limit_order

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
