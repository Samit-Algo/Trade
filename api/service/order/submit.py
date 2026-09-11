"""THE ONLY FILE THAT CAN SPEND MONEY.

Every call to `place_order` in this project is in this file, and each one is
preceded immediately by `assert_order_allowed`. That guard is the last thing
that runs before the wire. `settings` is a frozen dataclass, so nothing
between the guard and the call can change the mode or clear the dry-run flag.

If you are reviewing the safety of this project, read this file. It is the
whole submission path.
"""

from __future__ import annotations

from ..core.broker import PLACE_ORDER_LIMITER
from ..core.safety import assert_order_allowed
from ..market import QuoteSnapshot
from .bracket import (
    BracketLegs, build_option_order_with_bracket,
    validate_bracket_prices,
)
from .build import (
    DEFAULT_TIME_IN_FORCE, RULE_WIDTH, build_option_order, format_money,
)
from .cost import CostEstimate, compare_to_available_cash, estimate_cost
from .status import (
    DEFAULT_POLL_ATTEMPTS, DEFAULT_POLL_DELAY_SECONDS, FillOutcome,
    OrderSubmissionError, poll_until_settled,
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
        4. typed confirmation of the cash amount
        5. build the order
        6. assert_order_allowed(mode, dry_run)  <- THE GUARD
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

    from .cost import compare_to_available_cash

    cash_warning = None
    if estimate.is_buy:
        cash_warning = compare_to_available_cash(estimate.total_cash, cash_available)


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

    # THE GUARD. The last thing that happens before the wire, and the only
    # place it needs to happen: settings is a frozen dataclass, so nothing
    # between here and place_order can change the mode or the dry-run flag.
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
    checks inserted into step 3. There is no path around the guard: this
    function calls assert_order_allowed immediately before place_order,
    exactly as _submit_option_order does, and for the same reason.

        1. contract already resolved by the caller (Phase 3)
        2. quote already obtained and checked (Phase 4)
        3. print the full preview, including the bracket block
        4. typed confirmation of the cash amount
        5. build the order, with legs
        6. assert_order_allowed(mode, dry_run)  <- THE GUARD
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
    from .cost import compare_to_available_cash

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

    # THE GUARD -- the last thing before the wire. See the note above.
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
