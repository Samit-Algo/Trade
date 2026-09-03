"""Phase 5 -- place one real order on the paper account.

    python scripts/05_paper_order.py AAPL 2026-09-18 360 CALL BUY 1
    python scripts/05_paper_order.py --status 12345678
    python scripts/05_paper_order.py --cancel 12345678

This submits. Every other script in this project is read-only; this one is not.

Three locks stand between this script and an order reaching Tiger, and all
three must be satisfied:

    Lock 1  the configured account must equal TIGER_PAPER_ACCOUNT
    Lock 2  TIGER_ALLOW_LIVE must be true for any other account
    Lock 3  DRY_RUN must be false

With the shipped defaults nothing can be sent. DRY_RUN=false is a deliberate
act, taken knowing what it permits.

An order ID confirms submission, not execution. This script polls afterwards
and reports what actually filled, which is not always what was asked for.
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
    fetch_underlying_price,
)
from tiger_backend.orders import (  # noqa: E402
    DEFAULT_POLL_ATTEMPTS,
    DEFAULT_POLL_DELAY_SECONDS,
    DEFAULT_TIME_IN_FORCE,
    BracketError,
    OrderSubmissionError,
    buy_option,
    buy_option_with_bracket,
    get_attached_legs,
    print_attached_legs,
    cancel_order,
    get_order_status,
    normalise_status,
    print_fill_outcome,
    sell_option,
)
from tiger_backend.pricing import PricingError  # noqa: E402
from tiger_backend.providers import (  # noqa: E402
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    QuoteEntryError,
    build_market_data_provider,
)
from tiger_backend.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402
from tiger_backend.throttle import PRIME_ASSETS_LIMITER  # noqa: E402

RULE_WIDTH = 60

SECURITIES_SEGMENT = "S"


def fetch_cash_available(trade_client) -> float | None:
    """Fetch cash available to trade, not buying power.

    Buying power on this Reg T margin account is about four times the cash,
    and the difference is borrowed. An option can go to zero on its own; a
    loan taken to buy it does not.

    Args:
        trade_client: A tigeropen TradeClient.

    Returns:
        Cash available, or None if it could not be read.
    """
    PRIME_ASSETS_LIMITER.wait()
    try:
        portfolio = trade_client.get_prime_assets(base_currency="USD")
    except Exception:
        return None

    segments = getattr(portfolio, "segments", None) or {}
    segment = segments.get(SECURITIES_SEGMENT)
    if segment is None:
        return None

    cash = getattr(segment, "cash_available_for_trade", None)
    return float(cash) if cash is not None else None


def fetch_underlying_price_safely(quote_client, underlying: str):
    """Fetch the underlying price, tolerating its absence."""
    try:
        return fetch_underlying_price(quote_client, underlying)
    except Exception:
        return None


def get_fresh_quote(provider, contract, max_age_seconds: int):
    """Get a quote, re-prompting if it goes stale before it is used.

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
        print("  The market may have moved. Read it again.")
        print("-" * RULE_WIDTH)


def show_order_status(trade_client, order_id: int) -> int:
    """Print the current state of one order.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to look up.

    Returns:
        A process exit code.
    """
    order = get_order_status(trade_client, order_id)
    if order is None:
        print(f"The broker returned nothing for order {order_id}.")
        return 4

    status = normalise_status(getattr(order, "status", None))
    filled = getattr(order, "filled", 0)
    quantity = getattr(order, "quantity", 0)
    average = getattr(order, "avg_fill_price", None)

    print("=" * RULE_WIDTH)
    print(f"  ORDER {order_id}")
    print("=" * RULE_WIDTH)
    print(f"  Status         : {status}")
    print(f"  Filled         : {filled} of {quantity}")
    print(f"  Avg fill price : {average}")
    print(f"  Limit price    : {getattr(order, 'limit_price', None)}")
    print(f"  Reason         : {getattr(order, 'reason', '') or '-'}")
    print("=" * RULE_WIDTH)
    print("  Status alone does not tell you what filled. Read the filled count.")
    return 0


def cancel_and_report(trade_client, order_id: int, arguments) -> int:
    """Cancel one order and report what it actually ended up doing.

    Args:
        trade_client: A tigeropen TradeClient.
        order_id: The order to cancel.
        arguments: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    existing = get_order_status(trade_client, order_id)
    requested_quantity = int(getattr(existing, "quantity", 0) or 0) if existing else 0

    print(f"Cancelling order {order_id}...")
    outcome = cancel_order(
        trade_client=trade_client,
        order_id=order_id,
        requested_quantity=requested_quantity,
        poll_attempts=arguments.poll_attempts,
        poll_delay_seconds=arguments.poll_delay,
    )
    print_fill_outcome(outcome)
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface."""
    parser = argparse.ArgumentParser(
        description="Phase 5 -- place a real order on the paper account.",
    )
    parser.add_argument("underlying", nargs="?", help="Underlying symbol, e.g. AAPL")
    parser.add_argument("expiry", nargs="?", help="Expiry as YYYY-MM-DD")
    parser.add_argument("strike", nargs="?", type=float, help="Strike price")
    parser.add_argument("option_type", nargs="?", help="CALL or PUT")
    parser.add_argument("action", nargs="?", help="BUY or SELL")
    parser.add_argument("quantity", nargs="?", type=int, help="Number of contracts")

    parser.add_argument("--status", type=int, help="show the state of one order and exit")
    parser.add_argument("--cancel", type=int, help="cancel one order and exit")
    parser.add_argument("--legs", type=int, help="show the legs attached to one order and exit")

    parser.add_argument(
        "--take-profit",
        type=float,
        help="attach a take-profit leg at this price (requires --stop-loss)",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        help="attach a stop-loss leg at this price (requires --take-profit)",
    )
    parser.add_argument(
        "--leg-tif",
        default=DEFAULT_TIME_IN_FORCE,
        help=(
            "time in force for the attached legs (default DAY). Whether a "
            "paper account accepts GTC on a leg is undocumented and could "
            "not be established without submitting one."
        ),
    )

    parser.add_argument(
        "--max-quote-age",
        type=int,
        default=DEFAULT_MAX_QUOTE_AGE_SECONDS,
        help=f"seconds a typed quote stays usable (default {DEFAULT_MAX_QUOTE_AGE_SECONDS})",
    )
    parser.add_argument(
        "--poll-attempts",
        type=int,
        default=DEFAULT_POLL_ATTEMPTS,
        help=f"status checks after submission (default {DEFAULT_POLL_ATTEMPTS})",
    )
    parser.add_argument(
        "--poll-delay",
        type=float,
        default=DEFAULT_POLL_DELAY_SECONDS,
        help=f"seconds between checks (default {DEFAULT_POLL_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--liquidity-threshold",
        type=int,
        default=DEFAULT_LIQUIDITY_THRESHOLD,
        help="flag thin below this",
    )
    parser.add_argument("--grab", action="store_true", help="claim device access")
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    return parser


def place_order_flow(settings, quote_client, trade_client, arguments) -> int:
    """Resolve, quote, preview, confirm, submit and poll.

    Args:
        settings: Validated configuration.
        quote_client: A tigeropen QuoteClient.
        trade_client: A tigeropen TradeClient.
        arguments: Parsed command line arguments.

    Returns:
        A process exit code.
    """
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

    underlying_price = fetch_underlying_price_safely(quote_client, contract.underlying)
    cash_available = fetch_cash_available(trade_client)

    def record_submission(order_id, estimate):
        """Write the audit record the moment an ID exists.

        Written before polling on purpose. If polling then dies, the trail
        still shows that an order went out, which is the fact that matters.
        """
        record = build_order_record(
            settings=settings,
            contract=contract,
            quote=quote,
            estimate=estimate,
            stage="SUBMITTED",
            order_id=order_id,
        )
        record["submitted"] = True
        write_order_record(record)

    action = arguments.action.strip().upper()
    wants_bracket = (
        arguments.take_profit is not None or arguments.stop_loss is not None
    )

    shared = dict(
        trade_client=trade_client,
        settings=settings,
        contract=contract,
        quote=quote,
        quantity=arguments.quantity,
        underlying_price=underlying_price,
        cash_available=cash_available,
        liquidity_threshold=arguments.liquidity_threshold,
        max_age_seconds=arguments.max_quote_age,
        poll_attempts=arguments.poll_attempts,
        poll_delay_seconds=arguments.poll_delay,
        on_submitted=record_submission,
    )

    print()
    legs = None
    if wants_bracket:
        outcome, estimate, legs = buy_option_with_bracket(
            take_profit_price=arguments.take_profit,
            stop_loss_price=arguments.stop_loss,
            leg_time_in_force=arguments.leg_tif,
            **shared,
        )
    else:
        order_function = buy_option if action == "BUY" else sell_option
        outcome, estimate = order_function(**shared)

    print()
    print_fill_outcome(outcome, estimate)

    final_record = build_order_record(
        settings=settings,
        contract=contract,
        quote=quote,
        estimate=estimate,
        stage="FINAL",
        order_id=outcome.order_id,
        outcome=outcome.outcome,
        legs=legs,
    )
    final_record["fill"] = {
        "status": outcome.status,
        "filled_quantity": outcome.filled_quantity,
        "requested_quantity": outcome.requested_quantity,
        "average_fill_price": outcome.average_fill_price,
        "actual_cash": outcome.actual_cash,
        "reached_terminal_status": outcome.reached_terminal_status,
        "poll_attempts": outcome.poll_attempts,
        "reason": outcome.reason,
    }
    final_record["submitted"] = True
    log_path = write_order_record(final_record)

    print(f"Audit record written to {log_path.name}")

    # Legs only exist once the parent is on the broker's books, so this is
    # asked after submission rather than assumed from what was sent.
    if legs is not None and outcome.order_id is not None:
        print()
        print_attached_legs(
            get_attached_legs(trade_client, outcome.order_id), outcome.order_id
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the script."""
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

        if arguments.status is not None:
            return show_order_status(trade_client, arguments.status)

        if arguments.cancel is not None:
            return cancel_and_report(trade_client, arguments.cancel, arguments)

        if arguments.legs is not None:
            print_attached_legs(
                get_attached_legs(trade_client, arguments.legs), arguments.legs
            )
            return 0

        if (arguments.take_profit is None) != (arguments.stop_loss is None):
            parser.error(
                "--take-profit and --stop-loss must be given together: a "
                "bracket needs both sides"
            )

        required = (
            arguments.underlying,
            arguments.expiry,
            arguments.strike,
            arguments.option_type,
            arguments.action,
            arguments.quantity,
        )
        if any(value is None for value in required):
            parser.error(
                "give UNDERLYING EXPIRY STRIKE TYPE ACTION QUANTITY, "
                "or use --status / --cancel / --legs"
            )

        print(f"Market data source: {settings.market_data_source.upper()}")
        return place_order_flow(settings, quote_client, trade_client, arguments)

    except LiveTradingBlocked as error:
        print()
        print("=" * RULE_WIDTH)
        print("  BLOCKED BY THE SAFETY GUARD - NOTHING WAS SENT")
        print("=" * RULE_WIDTH)
        print(f"  {error}")
        print("=" * RULE_WIDTH)
        return 2
    except BracketError as error:
        print()
        print("THE BRACKET PRICES DO NOT WORK - NOTHING WAS SENT")
        print("-" * RULE_WIDTH)
        for line in str(error).splitlines():
            print(f"  {line}")
        print("-" * RULE_WIDTH)
        return 7
    except OrderSubmissionError as error:
        print()
        print("ORDER NOT SUBMITTED")
        print("-" * RULE_WIDTH)
        for line in str(error).splitlines():
            print(f"  {line}")
        print("-" * RULE_WIDTH)
        return 6
    except QuoteEntryError as error:
        print()
        print("QUOTE ENTRY STOPPED - NOTHING WAS SENT")
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
        print(f"  {error}")
        return 4
    except ClientSetupError as error:
        print()
        print("COULD NOT BUILD THE TIGER CLIENT")
        print(error)
        if arguments.debug:
            traceback.print_exc()
        return 3
    except Exception as error:  # noqa: BLE001 - top-level handler, message first
        print()
        print("THE ORDER FLOW FAILED")
        print("-" * RULE_WIDTH)
        print(f"{type(error).__name__}: {error}")
        print("-" * RULE_WIDTH)
        print("If an order ID was printed above, an order MAY be live at the")
        print("broker. Check it with --status before doing anything else.")
        if arguments.debug:
            print()
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
