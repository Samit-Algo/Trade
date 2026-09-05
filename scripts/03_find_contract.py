"""Phase 3 -- resolve a human request into one verified contract.

    python scripts/03_find_contract.py AAPL 2026-09-18 320 CALL
    python scripts/03_find_contract.py AAPL 2026-09-18 320 PUT

Read-only, and identity only. This prints what the contract IS. It prints no
bid, no ask, and no cost, because nothing has asked for those yet -- that is
Phase 4, through a MarketDataProvider.

No order is built or sent. Nothing here can spend money.
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
from api.service.contract import (  # noqa: E402
    ContractError,
    ExpiredContractError,
    ExpiryNotListedError,
    OptionContractInfo,
    StrikeNotFoundError,
    find_option_contract,
)
from api.service.core.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402

RULE_WIDTH = 68


def print_contract(contract: OptionContractInfo) -> None:
    """Print a resolved contract.

    Args:
        contract: The verified contract.
    """
    print("=" * RULE_WIDTH)
    print(f"  RESOLVED: {contract.describe()}")
    print("=" * RULE_WIDTH)
    print(f"  Identifier      : {contract.identifier}")
    print(f"  Underlying      : {contract.underlying}  ({contract.name})")
    print(f"  Expiry          : {contract.expiry_date_text}  "
          f"/ {contract.expiry_compact}")
    print(f"  Days to expiry  : {contract.days_to_expiry}")
    print(f"  Strike          : {contract.strike:,.2f}")
    print(f"  Type            : {contract.put_call}")
    print(f"  Multiplier      : {contract.multiplier:,.0f}"
          f"  ({contract.shares_per_contract:,.0f} shares per contract)")
    print(f"  Contract ID     : {contract.contract_id}")
    print(f"  Min tick        : {contract.min_tick}"
          "   (Tiger returns none; limit price is typed in Phase 4)")
    print("=" * RULE_WIDTH)
    print()
    print("  Identity only. No bid, ask, volume or open interest has been")
    print("  fetched or entered -- that is Phase 4, via a MarketDataProvider.")
    print()


def print_failure(heading: str, error: Exception) -> None:
    """Print a resolution failure in a readable block.

    Args:
        heading: A short label for the kind of failure.
        error: The exception raised.
    """
    print("-" * RULE_WIDTH)
    print(f"  {heading}")
    print("-" * RULE_WIDTH)
    for line in str(error).splitlines():
        print(f"  {line}")
    print("-" * RULE_WIDTH)
    print()


def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Phase 3 -- resolve one verified option contract.",
    )
    parser.add_argument("underlying", help="Underlying symbol, e.g. AAPL")
    parser.add_argument("expiry", help="Expiry as YYYY-MM-DD, e.g. 2026-09-18")
    parser.add_argument("strike", type=float, help="Strike price, e.g. 320")
    parser.add_argument("option_type", help="CALL or PUT")
    parser.add_argument(
        "--grab",
        action="store_true",
        help=(
            "claim market data device access, taking it from whatever held it. "
            "Not needed here; contract lookup does not use quote data."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the resolver.

    Args:
        argv: Command line arguments, or None to read them from sys.argv.

    Returns:
        A process exit code. 0 resolved, 4 could not be resolved, and 2 or 3
        for configuration and client problems.
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
        print_contract(contract)

    except ExpiredContractError as error:
        print_failure("CONTRACT HAS EXPIRED", error)
        return 4
    except ExpiryNotListedError as error:
        print_failure("EXPIRY NOT LISTED", error)
        return 4
    except StrikeNotFoundError as error:
        print_failure("STRIKE NOT FOUND", error)
        return 4
    except ContractError as error:
        print_failure("COULD NOT RESOLVE THE CONTRACT", error)
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
        print("THE LOOKUP FAILED")
        print("-" * RULE_WIDTH)
        print(f"{type(error).__name__}: {error}")
        if "permission denied" in str(error).lower():
            print()
            print("Contract lookup is normally free. Run")
            print("scripts/00_check_capabilities.py to see what this account reaches.")
        print("-" * RULE_WIDTH)
        print("Re-run with --debug for the full traceback.")
        if arguments.debug:
            print()
            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
