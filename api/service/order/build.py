"""Building an order object, and showing it to a human first.

Nothing here reaches Tiger. Everything in this file is safe to run at any
time: it constructs the order and prints what it would cost.
"""

from __future__ import annotations

from tigeropen.common.util.contract_utils import option_contract
from tigeropen.common.util.order_utils import limit_order

from ..market import QuoteSnapshot, calculate_spread, is_low_liquidity
from .cost import CostEstimate, compare_to_available_cash, estimate_cost


RULE_WIDTH = 60

#: Paper accounts do not support GTC, and this is the SDK's own default.
DEFAULT_TIME_IN_FORCE = "DAY"


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
    from ..market import calculate_spread

    _spread, spread_percent = calculate_spread(quote.bid, quote.ask)
    if spread_percent is None:
        return f"(ask {quote.ask:,.2f}, bid {quote.bid:,.2f})"
    return (
        f"(ask {quote.ask:,.2f}, bid {quote.bid:,.2f}, "
        f"spread {spread_percent:.1f}%)"
    )


def _describe_liquidity(quote: QuoteSnapshot, threshold: int) -> str:
    """Describe volume and open interest, flagging thin contracts."""
    from ..market import is_low_liquidity

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
    from .cost import compare_to_available_cash

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
