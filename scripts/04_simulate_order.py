"""Phase 4 -- resolve a contract, cost an order, and send nothing.

    python scripts/04_simulate_order.py AAPL 2026-09-18 320 CALL BUY 1
    python scripts/04_simulate_order.py AAPL 2026-09-18 300 PUT SELL 2

End to end: resolve the contract (Phase 3), get market data from a
MarketDataProvider, estimate the cost, print the preview, write the audit
record, exit.

**Nothing is submitted.** No submission call exists anywhere in this build.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tiger_backend.audit import build_order_record, write_order_record  # noqa: E402
from tiger_backend.clients import (  # noqa: E402
    ClientSetupError,
    build_quote_client,
    build_trade_client,
)
from tiger_backend.config import ConfigError, load_settings  # noqa: E402
from tiger_backend.contracts import ContractError, find_option_contract  # noqa: E402
from tiger_backend.market import (  # noqa: E402
    DEFAULT_LIQUIDITY_THRESHOLD,
    MarketDataError,
    fetch_underlying_price_safely,
)
from tiger_backend.orders import simulate_order  # noqa: E402
from tiger_backend.positions import fetch_cash_available  # noqa: E402
from tiger_backend.pricing import PricingError  # noqa: E402
from tiger_backend.providers import (  # noqa: E402
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    QuoteEntryError,
    build_market_data_provider,
)
from tiger_backend.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402

RULE_WIDTH = 60



def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Phase 4 -- simulate an option order. Sends nothing.",
    )
    parser.add_argument("underlying", help="Underlying symbol, e.g. AAPL")
    parser.add_argument("expiry", help="Expiry as YYYY-MM-DD")
    parser.add_argument("strike", type=float, help="Strike price")
    parser.add_argument("option_type", help="CALL or PUT")
    parser.add_argument("action", help="BUY or SELL")
    parser.add_argument("quantity", type=int, help="Number of contracts")
    parser.add_argument(
        "--max-quote-age",
        type=int,
        default=DEFAULT_MAX_QUOTE_AGE_SECONDS,
        help=(
            "seconds a typed quote stays usable before it must be re-entered "
            f"(default {DEFAULT_MAX_QUOTE_AGE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--liquidity-threshold",
        type=int,
        default=DEFAULT_LIQUIDITY_THRESHOLD,
        help=f"flag thin below this (default {DEFAULT_LIQUIDITY_THRESHOLD})",
    )
    parser.add_argument(
        "--grab",
        action="store_true",
        help="claim market data device access; not needed for this script",
    )
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    return parser


def get_fresh_quote(provider, contract, max_age_seconds: int):
    """Get a quote, re-prompting if it goes stale before it is used.

    A typed quote is stale the moment it is entered: you read the app, type
    five numbers, think, confirm, and the market has moved. Past the limit this
    re-prompts rather than warning, because a warning that can be scrolled past
    is not a control.

    Args:
        provider: A MarketDataProvider.
        contract: The OptionContractInfo.
        max_age_seconds: The staleness limit.

    Returns:
        A QuoteSnapshot that was fresh when returned.
    """
    while True:
        quote = provider.get_quote(contract)

        if not quote.is_stale(max_age_seconds):
            return quote

        print()
        print("-" * RULE_WIDTH)
        print(
            f"  QUOTE IS STALE: entered {quote.age_seconds():.0f}s ago, "
            f"limit is {max_age_seconds}s."
        )
        print("  The market may have moved. Please read and enter it again.")
        print("-" * RULE_WIDTH)


def main(argv: list[str] | None = None) -> int:
    """Run the simulation.

    Args:
        argv: Command line arguments, or None to read them from sys.argv.

    Returns:
        A process exit code.
    """
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
        print(f"Market data source: {settings.market_data_source.upper()}")

        quote_client = build_quote_client(settings, grab_permission=arguments.grab)
        trade_client = build_trade_client(settings)

        contract = find_option_contract(
            quote_client,
            trade_client,
            arguments.underlying,
            arguments.option_type,
            arguments.strike,
            arguments.expiry,
        )
        print(f"Resolved: {contract.identifier}")

        provider = build_market_data_provider(settings, quote_client=quote_client)
        quote = get_fresh_quote(provider, contract, arguments.max_quote_age)

        underlying_price = fetch_underlying_price_safely(
            quote_client, contract.underlying
        )
        cash_available = fetch_cash_available(trade_client)

        print()
        estimate, _order = simulate_order(
            settings=settings,
            contract=contract,
            quote=quote,
            action=arguments.action,
            quantity=arguments.quantity,
            underlying_price=underlying_price,
            cash_available=cash_available,
            liquidity_threshold=arguments.liquidity_threshold,
            max_age_seconds=arguments.max_quote_age,
        )

        record = build_order_record(
            settings=settings,
            contract=contract,
            quote=quote,
            estimate=estimate,
            stage="SIMULATED",
        )
        log_path = write_order_record(record)

        print(f"Audit record written to {log_path.name}")
        print("Nothing was sent. No submission code exists in this build.")
        print()

    except QuoteEntryError as error:
        print()
        print("QUOTE ENTRY STOPPED")
        print("-" * RULE_WIDTH)
        for line in str(error).splitlines():
            print(f"  {line}")
        print("-" * RULE_WIDTH)
        return 5
    except ContractError as error:
        print()
        print("COULD NOT RESOLVE THE CONTRACT")
        print("-" * RULE_WIDTH)
        for line in str(error).splitlines():
            print(f"  {line}")
        print("-" * RULE_WIDTH)
        return 4
    except PricingError as error:
        print()
        print("COULD NOT ESTIMATE THE COST")
        print("-" * RULE_WIDTH)
        print(f"  {error}")
        print("-" * RULE_WIDTH)
        return 4
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
        print("THE SIMULATION FAILED")
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
