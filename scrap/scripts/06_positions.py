"""Phase 6 -- show what you hold and what it is worth.

    python scripts/06_positions.py
    python scripts/06_positions.py --expiry-warning-days 5
    python scripts/06_positions.py --no-valuation      # list only, type nothing

Read-only. Nothing here closes a position or builds an order.

Positions are valued at the BID, because a position is worth what someone
will pay for it. Under manual entry the bid is typed, labelled [MANUAL], and
subject to the same staleness limit as everything else typed by hand.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.core.broker import (  # noqa: E402
    ClientSetupError,
    build_quote_client,
    build_trade_client,
)
from api.service.core.config import ConfigError, load_settings  # noqa: E402
from api.service.position import (  # noqa: E402
    DEFAULT_EXPIRY_WARNING_DAYS,
    OptionPosition,
    PositionValuation,
    build_expiry_warning,
    is_expiring_soon,
    list_option_positions,
    value_position,
)
from api.service.market import (  # noqa: E402
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    QuoteEntryError,
    build_market_data_provider,
)
from api.service.core.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402

RULE_WIDTH = 74


def format_money(value: float | None) -> str:
    """Format a cash amount, or "-" when there is none."""
    if value is None:
        return "-"
    return f"${value:,.2f}"


def format_signed_money(value: float) -> str:
    """Format a profit or loss with an explicit sign."""
    return f"{value:+,.2f}"


def print_position_list(positions: list[OptionPosition], threshold_days: int) -> None:
    """Print the positions held, before any valuation.

    Args:
        positions: The positions.
        threshold_days: Below this, days-to-expiry is flagged.
    """
    print("=" * RULE_WIDTH)
    print(f"  OPTION POSITIONS HELD: {len(positions)}")
    print("=" * RULE_WIDTH)
    print(
        f"{'CONTRACT':<24}{'TYPE':>6}{'STRIKE':>10}"
        f"{'QTY':>6}{'AVG COST':>11}{'DTE':>6}"
    )
    print("-" * RULE_WIDTH)

    for position in positions:
        flag = "  <-- EXPIRING" if is_expiring_soon(
            position.days_to_expiry, threshold_days
        ) else ""
        print(
            f"{position.identifier:<24}"
            f"{position.put_call:>6}"
            f"{position.strike:>10,.2f}"
            f"{position.quantity:>6,.0f}"
            f"{position.average_cost:>11,.4f}"
            f"{position.days_to_expiry:>6}"
            f"{flag}"
        )

    print("-" * RULE_WIDTH)
    print("  AVG COST is per share and INCLUDES commission, as Tiger reports it.")
    print()


def print_valuation(valuation: PositionValuation, max_age_seconds: int) -> None:
    """Print one position's profit and loss, with the arithmetic shown.

    Args:
        valuation: The valued position.
        max_age_seconds: The staleness limit, for context.
    """
    position = valuation.position

    print("=" * RULE_WIDTH)
    print(f"  {position.describe()}")
    print("=" * RULE_WIDTH)
    print(f"  Contract       : {position.identifier}")
    print(f"  Quantity held  : {position.quantity:,.0f} contract(s)")
    print(f"  Days to expiry : {position.days_to_expiry}")
    print("-" * RULE_WIDTH)
    print(
        f"  Avg entry cost : {position.average_cost:,.4f} per share"
        "   (includes commission)"
    )
    print(
        f"  Current BID    : {valuation.current_bid:,.4f} per share "
        f"{valuation.bid_source_tag}"
    )
    print(
        f"                   entered {valuation.bid_age_seconds:.0f}s ago "
        f"(limit {max_age_seconds}s)"
    )
    print("-" * RULE_WIDTH)
    print(f"  Cost basis     : {format_money(valuation.cost_basis)}")
    print(
        f"  Worth now      : {format_money(valuation.current_value)}"
        "   (at the bid -- what a buyer will pay)"
    )
    print(
        f"  UNREALISED P&L : {format_signed_money(valuation.unrealised_pnl)}"
        + (
            f"   ({valuation.unrealised_pnl_percent:+.1f}%)"
            if valuation.unrealised_pnl_percent is not None
            else ""
        )
    )
    print(
        f"                   ({valuation.current_bid:,.4f} - "
        f"{position.average_cost:,.4f}) x {position.multiplier:,.0f} x "
        f"{position.quantity:,.0f}"
    )

    if position.tiger_unrealised_pnl is not None:
        print("-" * RULE_WIDTH)
        print(
            f"  Tiger reports  : {format_signed_money(position.tiger_unrealised_pnl)}"
        )
        print(
            f"                   Tiger values at market_price "
            f"({position.market_price_latest}), which is"
        )
        print("                   latestPrice -- the last trade someone else made.")
        print("                   The bid is what YOU could get. They differ by the")
        print("                   spread, and the bid is the honest one.")

    if valuation.bid_is_stale:
        print("-" * RULE_WIDTH)
        print("  NOTE: that bid is older than the staleness limit.")

    print("=" * RULE_WIDTH)


def print_expiry_warnings(positions: list[OptionPosition], threshold_days: int) -> bool:
    """Print the loud warning block for positions near expiry.

    Args:
        positions: The positions.
        threshold_days: Warn at or below this.

    Returns:
        True when at least one warning was printed.
    """
    expiring = [
        position
        for position in positions
        if is_expiring_soon(position.days_to_expiry, threshold_days)
    ]
    if not expiring:
        return False

    print()
    print("!" * RULE_WIDTH)
    print("!!  EXPIRY WARNING  --  READ THIS")
    print("!" * RULE_WIDTH)

    for position in expiring:
        for line in build_expiry_warning(position, threshold_days):
            print(f"!!  {line}" if line else "!!")
        print("!" * RULE_WIDTH)

    return True


def print_portfolio_total(valuations: list[PositionValuation]) -> None:
    """Print the combined figures across every valued position."""
    if not valuations:
        return

    total_cost = sum(item.cost_basis for item in valuations)
    total_value = sum(item.current_value for item in valuations)
    total_pnl = sum(item.unrealised_pnl for item in valuations)

    print()
    print("=" * RULE_WIDTH)
    print("  TOTAL, ALL VALUED POSITIONS")
    print("=" * RULE_WIDTH)
    print(f"  Cost basis     : {format_money(round(total_cost, 2))}")
    print(f"  Worth now      : {format_money(round(total_value, 2))}   (at the bid)")
    print(f"  UNREALISED P&L : {format_signed_money(round(total_pnl, 2))}")
    print("=" * RULE_WIDTH)


def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface."""
    parser = argparse.ArgumentParser(
        description="Phase 6 -- show option positions and their P&L. Read-only.",
    )
    parser.add_argument(
        "--expiry-warning-days",
        type=int,
        default=DEFAULT_EXPIRY_WARNING_DAYS,
        help=(
            "warn on positions with this many days left or fewer "
            f"(default {DEFAULT_EXPIRY_WARNING_DAYS})"
        ),
    )
    parser.add_argument(
        "--max-quote-age",
        type=int,
        default=DEFAULT_MAX_QUOTE_AGE_SECONDS,
        help=f"seconds a typed bid stays usable (default {DEFAULT_MAX_QUOTE_AGE_SECONDS})",
    )
    parser.add_argument(
        "--no-valuation",
        action="store_true",
        help="list positions without asking for bids; no P&L is computed",
    )
    parser.add_argument("--grab", action="store_true", help="claim device access")
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the report."""
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    try:
        settings = load_settings()
    except LiveTradingBlocked as error:
        print("BLOCKED BY THE SAFETY GUARD")
        print(error)
        return 2
    except ConfigError as error:
        print("CONFIGURATION PROBLEM")
        print(error)
        return 2

    try:
        print_startup_banner(settings.masked_account, settings.mode, settings.dry_run)
        print()

        trade_client = build_trade_client(settings)
        positions = list_option_positions(trade_client)

        if not positions:
            print("No option positions held.")
            return 0

        print_position_list(positions, arguments.expiry_warning_days)
        print_expiry_warnings(positions, arguments.expiry_warning_days)

        if arguments.no_valuation:
            print()
            print("Valuation skipped (--no-valuation). No bids were entered.")
            return 0

        quote_client = build_quote_client(settings, grab_permission=arguments.grab)
        provider = build_market_data_provider(settings, quote_client=quote_client)

        valuations = []
        for position in positions:
            bid_snapshot = provider.get_bid(position)
            valuation = value_position(
                position, bid_snapshot, arguments.max_quote_age
            )
            print()
            print_valuation(valuation, arguments.max_quote_age)
            valuations.append(valuation)

        print_portfolio_total(valuations)

    except QuoteEntryError as error:
        print()
        print("BID ENTRY STOPPED")
        print("-" * RULE_WIDTH)
        for line in str(error).splitlines():
            print(f"  {line}")
        print("-" * RULE_WIDTH)
        return 5
    except LiveTradingBlocked as error:
        print()
        print("BLOCKED BY THE SAFETY GUARD")
        print(error)
        return 2
    except ClientSetupError as error:
        print()
        print("COULD NOT BUILD THE TIGER CLIENT")
        print(error)
        if arguments.debug:
            traceback.print_exc()
        return 3
    except Exception as error:  # noqa: BLE001 - top-level handler, message first
        print()
        print("COULD NOT READ POSITIONS")
        print("-" * RULE_WIDTH)
        print(f"{type(error).__name__}: {error}")
        print("-" * RULE_WIDTH)
        print("Re-run with --debug for the full traceback.")
        if arguments.debug:
            print()
            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
