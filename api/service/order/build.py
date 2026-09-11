"""Building an order object, and showing it to a human first.

Nothing here reaches Tiger. Everything in this file is safe to run at any
time: it constructs the order and prints what it would cost.
"""

from __future__ import annotations

from tigeropen.common.util.contract_utils import option_contract
from tigeropen.common.util.order_utils import limit_order

from ..market import QuoteSnapshot, calculate_spread
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
    """Describe today's volume, flagging thin contracts."""
    from ..market import is_low_liquidity

    volume_text = f"{quote.volume:,}" if quote.volume is not None else "-"
    flag = "[THIN]" if is_low_liquidity(quote.volume, threshold) else "[OK]"

    return f"volume {volume_text}   {flag}"


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

