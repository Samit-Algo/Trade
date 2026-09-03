"""Phase 2 -- show a real option chain the way a human reads one.

    python scripts/02_show_chain.py AAPL
    python scripts/02_show_chain.py AAPL --all           # every strike
    python scripts/02_show_chain.py AAPL --strikes 5     # 5 either side
    python scripts/02_show_chain.py AAPL --expiry 2026-09-18

Read-only. This script only displays; every calculation lives in
tiger_backend/market.py so it can be tested without a network connection.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tiger_backend.clients import ClientSetupError, build_quote_client  # noqa: E402
from tiger_backend.config import ConfigError, load_settings  # noqa: E402
from tiger_backend.market import (  # noqa: E402
    DEFAULT_LIQUIDITY_THRESHOLD,
    MarketDataError,
    OptionExpiry,
    OptionRow,
    StrikeRow,
    UnderlyingPrice,
    build_option_rows,
    fetch_option_chain,
    fetch_underlying_price,
    find_atm_strike,
    list_expirations,
    pair_calls_and_puts_by_strike,
    select_strikes_around_price,
)
from tiger_backend.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402

#: How many strikes to show either side of the money unless asked otherwise.
DEFAULT_STRIKES_EACH_SIDE = 10

# Column widths. Both the data rows and the headings are built from these
# constants, so the two cannot drift apart. Widths are sized for the worst
# realistic case, not the typical one: near-the-money volume on a liquid name
# runs to seven figures, and "1,204,553" needs ten columns once the thousands
# separators are counted. An 8-wide volume column looks fine on quiet strikes
# and then silently collides with the column to its left on busy ones.
PRICE_WIDTH = 8
SPREAD_WIDTH = 8
COUNT_WIDTH = 10
IV_WIDTH = 7

#: Trailing " X" carrying the thin-liquidity marker.
MARKER_WIDTH = 2

#: Width of one formatted side of the table (calls or puts).
SIDE_WIDTH = (
    PRICE_WIDTH * 2 + SPREAD_WIDTH + COUNT_WIDTH * 2 + IV_WIDTH + MARKER_WIDTH
)

#: Width of the centre strike column.
STRIKE_WIDTH = 14

RULE_WIDTH = SIDE_WIDTH + STRIKE_WIDTH + SIDE_WIDTH


def format_price(value: float | None) -> str:
    """Format a money value for the table.

    Args:
        value: The number, or None when the API did not report one.

    Returns:
        The number to two decimal places, or "-" when there is no value.
        Never the word "nan", which is what pandas would otherwise print.
    """
    if value is None:
        return "-"
    return f"{value:,.2f}"


def format_count(value: int | None) -> str:
    """Format a whole-number count (volume, open interest) for the table.

    Args:
        value: The number, or None when the API did not report one.

    Returns:
        The number with thousands separators, or "-".
    """
    if value is None:
        return "-"
    return f"{value:,}"


def format_percent(value: float | None) -> str:
    """Format a percentage for the table.

    Args:
        value: The percentage, or None.

    Returns:
        The percentage to one decimal place with a % sign, or "-".
    """
    if value is None:
        return "-"
    return f"{value:.1f}%"


def format_implied_volatility(value: float | None) -> str:
    """Format implied volatility for the table.

    Tiger reports implied volatility as a decimal, so 0.284 means 28.4%.

    Args:
        value: The implied volatility as a decimal, or None.

    Returns:
        The value as a percentage, or "-".
    """
    if value is None:
        return "-"
    percentage = value * 100
    return f"{percentage:.1f}%"


def format_option_side(option_row: OptionRow | None) -> str:
    """Format one side of a chain row -- either the call or the put.

    Args:
        option_row: The contract, or None when the exchange lists no contract
            on that side at this strike.

    Returns:
        A fixed-width string so the columns line up on every row.
    """
    if option_row is None:
        return " " * SIDE_WIDTH

    bid_text = format_price(option_row.bid)
    ask_text = format_price(option_row.ask)
    spread_text = format_percent(option_row.spread_percent)
    volume_text = format_count(option_row.volume)
    open_interest_text = format_count(option_row.open_interest)
    implied_volatility_text = format_implied_volatility(option_row.implied_volatility)

    # A visible mark beats a colour: it survives copy and paste into a note.
    thin_marker = "!" if option_row.is_thin else " "

    side = (
        f"{bid_text:>{PRICE_WIDTH}}"
        f"{ask_text:>{PRICE_WIDTH}}"
        f"{spread_text:>{SPREAD_WIDTH}}"
        f"{volume_text:>{COUNT_WIDTH}}"
        f"{open_interest_text:>{COUNT_WIDTH}}"
        f"{implied_volatility_text:>{IV_WIDTH}}"
        f" {thin_marker}"
    )
    return f"{side:<{SIDE_WIDTH}}"


def format_strike(strike: float, is_at_the_money: bool) -> str:
    """Format the centre strike column, marking the at-the-money row.

    Args:
        strike: The strike price.
        is_at_the_money: True for the strike nearest the current share price.

    Returns:
        A fixed-width string, with markers around the ATM strike.
    """
    strike_text = f"{strike:,.2f}"
    if is_at_the_money:
        return f"> {strike_text:^10} <"
    return f"  {strike_text:^10}  "


def print_expiry_menu(expiries: list[OptionExpiry]) -> None:
    """Print the numbered list of expiration dates Tiger returned.

    Args:
        expiries: Expiries from list_expirations, soonest first.
    """
    print("AVAILABLE EXPIRATIONS")
    print("-" * 52)
    print(f"{'#':>3}  {'DATE':<12} {'DAYS':>5}  {'TYPE':<8}")
    print("-" * 52)

    for position, expiry in enumerate(expiries, start=1):
        # A negative day count means the date has already passed. Tiger
        # still returns it, so it is shown, but it is not tradable.
        if expiry.days_to_expiry < 0:
            note = "EXPIRED"
        else:
            note = ""

        print(
            f"{position:>3}  {expiry.date_text:<12} "
            f"{expiry.days_to_expiry:>5}  {expiry.period_label:<8} {note}"
        )

    print("-" * 52)
    print(f"{len(expiries)} expirations, all reported by Tiger. None are constructed.")
    print()


def ask_user_to_pick_expiry(expiries: list[OptionExpiry]) -> OptionExpiry:
    """Ask which expiry to show, repeating until the answer is on the list.

    Only offers dates the API returned, so an expiry that does not exist cannot
    be chosen.

    Args:
        expiries: The expiries to choose from.

    Returns:
        The chosen expiry.

    Raises:
        MarketDataError: If input ends before a choice is made.
    """
    while True:
        try:
            typed = input(f"Pick an expiration [1-{len(expiries)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise MarketDataError("No expiration chosen.") from None

        if not typed.isdigit():
            print("Please type one of the numbers in the # column.")
            continue

        chosen_number = int(typed)
        if chosen_number < 1 or chosen_number > len(expiries):
            print(f"Please type a number between 1 and {len(expiries)}.")
            continue

        return expiries[chosen_number - 1]


def find_expiry_by_date(
    expiries: list[OptionExpiry],
    wanted_date_text: str,
) -> OptionExpiry:
    """Look up an expiry the user named on the command line.

    Args:
        expiries: The expiries Tiger returned.
        wanted_date_text: The date the user asked for, as "YYYY-MM-DD".

    Returns:
        The matching expiry.

    Raises:
        MarketDataError: If that date is not one Tiger listed. The message
            lists the nearest real dates, because a date that is not listed
            simply does not exist as a contract.
    """
    for expiry in expiries:
        if expiry.date_text == wanted_date_text:
            return expiry

    available_dates = []
    for expiry in expiries[:8]:
        available_dates.append(expiry.date_text)

    raise MarketDataError(
        f"{wanted_date_text} is not a listed expiration. "
        f"Nearest available: {', '.join(available_dates)}"
    )


def print_table_header(
    underlying_price: UnderlyingPrice,
    expiry: OptionExpiry,
    shown_row_count: int,
    total_row_count: int,
) -> None:
    """Print the reference line and column headings above the chain.

    Args:
        underlying_price: Current price of the underlying share.
        expiry: The expiry being shown.
        shown_row_count: How many strikes are being printed.
        total_row_count: How many strikes exist for this expiry.
    """
    print("=" * RULE_WIDTH)
    print(
        f"  {underlying_price.symbol} @ ${underlying_price.price:,.2f}"
        f"  ({underlying_price.freshness_label})"
        f"   |   Expiry {expiry.date_text}"
        f"  ({expiry.days_to_expiry} days away, {expiry.period_label})"
    )
    print(f"  Showing {shown_row_count} of {total_row_count} strikes.")
    print("=" * RULE_WIDTH)

    calls_heading = f"{'CALLS':^{SIDE_WIDTH}}"
    puts_heading = f"{'PUTS':^{SIDE_WIDTH}}"
    print(f"{calls_heading}{'STRIKE':^{STRIKE_WIDTH}}{puts_heading}")

    # Built from the same constants as the data rows, in the same order.
    column_headings = (
        f"{'bid':>{PRICE_WIDTH}}"
        f"{'ask':>{PRICE_WIDTH}}"
        f"{'spread':>{SPREAD_WIDTH}}"
        f"{'vol':>{COUNT_WIDTH}}"
        f"{'OI':>{COUNT_WIDTH}}"
        f"{'IV':>{IV_WIDTH}}"
        f"{'':>{MARKER_WIDTH}}"
    )
    print(f"{column_headings:<{SIDE_WIDTH}}{'':^{STRIKE_WIDTH}}{column_headings:<{SIDE_WIDTH}}")
    print("-" * RULE_WIDTH)


def print_chain_table(strike_rows: list[StrikeRow], atm_strike: float | None) -> None:
    """Print the CALLS | STRIKE | PUTS table.

    Args:
        strike_rows: Rows to print, sorted by strike ascending.
        atm_strike: The strike nearest the share price, marked in the output.
    """
    for strike_row in strike_rows:
        is_at_the_money = strike_row.strike == atm_strike

        call_text = format_option_side(strike_row.call)
        strike_text = format_strike(strike_row.strike, is_at_the_money)
        put_text = format_option_side(strike_row.put)

        print(f"{call_text}{strike_text}{put_text}")

    print("-" * RULE_WIDTH)


def print_legend(liquidity_threshold: int) -> None:
    """Explain the table's markers and columns.

    Args:
        liquidity_threshold: The number below which a row is flagged thin.
    """
    print("  >strike<  at the money: the listed strike nearest the share price.")
    print(
        f"  !         thin: volume or open interest below {liquidity_threshold}. "
        "Wide spreads, and hard to sell later."
    )
    print("  spread    (ask - bid) as a percentage of the ask. You pay it on entry.")
    print("  IV        implied volatility: the move the market expects from here.")
    print()
    print("  Greeks (delta, gamma, theta, vega, rho) are deliberately not shown.")
    print("  Tiger deprecates them on this endpoint: they update once a day and")
    print("  are not suitable for intraday decisions.")
    print()


def show_chain(quote_client, underlying: str, arguments) -> int:
    """Fetch and print one option chain.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol, uppercased.
        arguments: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    expiries = list_expirations(quote_client, underlying)

    if arguments.expiry:
        chosen_expiry = find_expiry_by_date(expiries, arguments.expiry)
    else:
        print_expiry_menu(expiries)
        chosen_expiry = ask_user_to_pick_expiry(expiries)

    print()
    print(f"Fetching {underlying} chain for {chosen_expiry.date_text} ...")

    underlying_price = fetch_underlying_price(quote_client, underlying)

    # The date passed here came from list_expirations, never from a calendar
    # calculation, so it is guaranteed to be a real listed expiry.
    chain_frame = fetch_option_chain(quote_client, underlying, chosen_expiry.date_text)

    option_rows = build_option_rows(chain_frame, arguments.liquidity_threshold)
    all_strike_rows = pair_calls_and_puts_by_strike(option_rows)

    if arguments.all:
        rows_to_show = all_strike_rows
    else:
        rows_to_show = select_strikes_around_price(
            all_strike_rows,
            underlying_price.price,
            arguments.strikes,
        )

    atm_strike = find_atm_strike(all_strike_rows, underlying_price.price)

    print()
    print_table_header(
        underlying_price,
        chosen_expiry,
        len(rows_to_show),
        len(all_strike_rows),
    )
    print_chain_table(rows_to_show, atm_strike)
    print_legend(arguments.liquidity_threshold)

    if not arguments.all and len(rows_to_show) < len(all_strike_rows):
        print("  Run again with --all to see every strike.")
        print()

    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Phase 2 -- show a real option chain for an underlying.",
    )
    parser.add_argument("underlying", help="Underlying symbol, e.g. AAPL")
    parser.add_argument(
        "--all",
        action="store_true",
        help="show every strike instead of a window around the money",
    )
    parser.add_argument(
        "--strikes",
        type=int,
        default=DEFAULT_STRIKES_EACH_SIDE,
        help=f"strikes to show each side of the money (default {DEFAULT_STRIKES_EACH_SIDE})",
    )
    parser.add_argument(
        "--expiry",
        help="skip the menu and use this expiry, as YYYY-MM-DD",
    )
    parser.add_argument(
        "--liquidity-threshold",
        type=int,
        default=DEFAULT_LIQUIDITY_THRESHOLD,
        help=f"flag rows below this volume or open interest (default {DEFAULT_LIQUIDITY_THRESHOLD})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print full tracebacks instead of a plain message",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the script.

    Args:
        argv: Command line arguments, or None to read them from sys.argv.

    Returns:
        A process exit code: 0 on success, non-zero on failure.
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    underlying = arguments.underlying.upper()

    try:
        settings = load_settings()
    except LiveTradingBlocked as error:
        print("BLOCKED BY THE SAFETY GUARD")
        print("-" * 60)
        print(error)
        return 2
    except ConfigError as error:
        print("CONFIGURATION PROBLEM")
        print("-" * 60)
        print(error)
        return 2

    try:
        # The banner comes before anything else, on every entry point.
        print_startup_banner(settings.masked_account, settings.mode, settings.dry_run)
        print()

        quote_client = build_quote_client(settings)
        return show_chain(quote_client, underlying, arguments)

    except LiveTradingBlocked as error:
        print()
        print("BLOCKED BY THE SAFETY GUARD")
        print("-" * 60)
        print(error)
        return 2
    except ClientSetupError as error:
        print()
        print("COULD NOT BUILD THE TIGER CLIENT")
        print("-" * 60)
        print(error)
        if arguments.debug:
            traceback.print_exc()
        return 3
    except MarketDataError as error:
        print()
        print("NO USABLE MARKET DATA")
        print("-" * 60)
        print(error)
        return 4
    except Exception as error:  # noqa: BLE001 - top-level handler, message first
        print()
        print("THE MARKET DATA REQUEST FAILED")
        print("-" * 60)
        print(f"{type(error).__name__}: {error}")
        print()
        if "permission denied" in str(error).lower():
            print("This is a market data entitlement, not a bug in this code.")
            print("Option chains and option quotes need OpenAPI US OPTION market")
            print("data, which is purchased separately from the Tiger Trade app")
            print("or from Personal Center. Expiration dates are free and work.")
        else:
            print("Common causes, in the order worth checking:")
            print("  1. The symbol has no listed options, or is misspelled.")
            print("  2. Market data access for US options has not been purchased.")
            print("  3. The market is closed and this contract has no recent quotes.")
        print("-" * 60)
        print("Re-run with --debug for the full traceback.")
        if arguments.debug:
            print()
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
