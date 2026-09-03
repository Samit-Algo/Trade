"""Learning tool -- show how one option's premium actually moved over its life.

Standalone. Not part of the phase sequence, and nothing in Phases 3-6 imports it.

    python scripts/07_premium_history.py AAPL 2026-09-18 320 CALL
    python scripts/07_premium_history.py AAPL 2026-08-21 355 CALL --tail 20

Why this exists: the whole profit-and-loss model of an option rests on how its
premium behaves, and that is not obvious from a single snapshot. Watching a
close price decay day after day while the underlying barely moves teaches time
decay faster than any explanation.

What it shows is DAILY TRADED PRICES, not quotes. See the warning in the header
and read it properly -- it is the difference between understanding a contract
and mispricing an order.

Documented call used here, read-only:
  QuoteClient.get_option_bars(identifiers, begin_time=-1, end_time=4070880000000,
                              period=BarPeriod.DAY, limit=None, sort_dir=None,
                              market=None, timezone=None)          60 requests/min
https://docs-en.itigerup.com/docs/quote-option
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tiger_backend.clients import ClientSetupError, build_quote_client  # noqa: E402
from tiger_backend.config import ConfigError, load_settings  # noqa: E402
from tiger_backend.market import (  # noqa: E402
    MarketDataError,
    list_expirations,
    milliseconds_to_date,
)
from tiger_backend.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402
from tiger_backend.throttle import OPTION_BARS_LIMITER  # noqa: E402

RULE_WIDTH = 96

VALID_OPTION_TYPES = ("CALL", "PUT")


def to_tiger_expiry_format(date_text: str) -> str:
    """Convert "YYYY-MM-DD" to the "yyyyMMdd" form Tiger's helpers want.

    Tiger's contract functions take the compact form while its expiration list
    returns the dashed one. Mixing them up is the most common bug in this
    integration.

    This is a local copy on purpose. Phase 3 owns the canonical version in
    contracts.py, and this standalone tool must not pre-empt it.

    Args:
        date_text: An expiry as "YYYY-MM-DD".

    Returns:
        The same date as "yyyyMMdd".

    Raises:
        MarketDataError: If the text is not in the expected format.
    """
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as error:
        raise MarketDataError(
            f"Could not read {date_text!r} as a date (expected YYYY-MM-DD)."
        ) from error
    return parsed.strftime("%Y%m%d")


def verify_expiry(quote_client, underlying: str, expiry_date_text: str) -> bool:
    """Check the expiry against the dates Tiger actually lists.

    Expiries are never constructed in this project: a date built from a
    calendar rule can look entirely plausible and simply not exist.

    There is one honest exception, and this tool is it. Tiger lists only
    currently-tradable expiries, so an expired contract can never appear in
    that list even though its history is real and fetchable. Rejecting past
    dates outright would leave a historical viewer unable to view history.

    So the rule here has three branches:

      - listed                      -> verified, proceed
      - not listed, date in future  -> a typo. Refuse, and list the real dates.
      - not listed, date in past    -> unverifiable, but not wrong. Proceed and
                                       say so. get_option_bars is then the
                                       arbiter, because a contract that never
                                       existed returns no bars at all.

    No money can move through this script, so proceeding unverified on a past
    date risks nothing worse than an empty table.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol.
        expiry_date_text: The expiry the user asked for, as "YYYY-MM-DD".

    Returns:
        True if the expiry was found in Tiger's list; False if it is a past
        date that could not be verified.

    Raises:
        MarketDataError: If the date is in the future and is not listed.
    """
    expiries = list_expirations(quote_client, underlying)

    for expiry in expiries:
        if expiry.date_text == expiry_date_text:
            return True

    wanted_date = datetime.strptime(expiry_date_text, "%Y-%m-%d").date()
    if wanted_date < date.today():
        return False

    nearest_dates = []
    for expiry in expiries[:8]:
        nearest_dates.append(expiry.date_text)

    raise MarketDataError(
        f"{expiry_date_text} is not a listed expiration for {underlying}, and it "
        "is not in the past either, so it is not a contract that once existed.\n"
        f"Nearest available: {', '.join(nearest_dates)}"
    )


def build_contract_identifier(
    underlying: str,
    expiry_date_text: str,
    strike: float,
    option_type: str,
) -> str:
    """Build the option identifier using the SDK's own helper.

    The 21-character identifier is never assembled with string formatting. The
    padding and scaling rules are fiddly, and a hand-built identifier that is
    subtly wrong looks correct and refers to nothing.

    Args:
        underlying: Underlying symbol, e.g. "AAPL".
        expiry_date_text: Expiry as "YYYY-MM-DD".
        strike: Strike price.
        option_type: "CALL" or "PUT".

    Returns:
        The contract identifier.
    """
    from tigeropen.common.util.contract_utils import get_option_identifier

    compact_expiry = to_tiger_expiry_format(expiry_date_text)
    return get_option_identifier(underlying, compact_expiry, option_type, strike)


def fetch_daily_bars(quote_client, identifier: str):
    """Fetch every daily bar for one contract, over its whole life.

    No `limit` is passed. The parameter exists in the API but was measured to
    have no effect on daily option bars -- asking for 5 returned all 61 -- so
    sending it would only imply a truncation that does not happen. The default
    begin_time (-1, earliest) and end_time (far future) already span the
    contract's entire life, including contracts that have already expired.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: The contract identifier.

    Returns:
        The pandas DataFrame the SDK returned, oldest bar first.

    Raises:
        MarketDataError: If no bars come back.
    """
    from tigeropen.common.consts import BarPeriod, Market

    OPTION_BARS_LIMITER.wait()

    bars_frame = quote_client.get_option_bars(
        identifiers=[identifier],
        period=BarPeriod.DAY,
        market=Market.US,
    )

    # A contract with no history comes back as an empty *list*, not an empty
    # DataFrame, so testing .empty alone would raise AttributeError and hide
    # the real answer behind a confusing error.
    if bars_frame is None or isinstance(bars_frame, list) or bars_frame.empty:
        raise MarketDataError(
            f"No price history for {identifier.strip()}.\n"
            "The expiry is listed, so the likely cause is a strike that does not "
            "exist for it, or a contract that has simply never traded."
        )

    # Sort oldest first, explicitly. The "VS 1ST" column measures every close
    # against the earliest one shown, so the row order must not depend on how
    # the API happened to return them.
    if "time" in bars_frame.columns:
        bars_frame = bars_frame.sort_values("time", ascending=True)

    return bars_frame


def read_optional_number(row, column_name: str):
    """Read one column of a DataFrame row, or None when it is missing or empty.

    pandas writes a missing number as NaN, which is a float and so survives an
    `is None` check before poisoning any arithmetic it touches.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.

    Returns:
        The value, or None.
    """
    import pandas

    if column_name not in row:
        return None

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return None

    return raw_value


def format_price(value) -> str:
    """Format a premium for the table.

    Args:
        value: The price, or None.

    Returns:
        The price to two decimal places, or "-".
    """
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


def format_count(value) -> str:
    """Format a whole-number count for the table.

    Args:
        value: The count, or None.

    Returns:
        The count with thousands separators, or "-".
    """
    if value is None:
        return "-"
    return f"{int(value):,}"


def format_relative(close_price, first_close_price) -> str:
    """Express a close as a percentage of the first day's close.

    100% is where the contract started. Below 100% is a loss to a holder who
    bought on the first day shown; above is a gain. Reading down this column is
    the quickest way to see time decay at work.

    Args:
        close_price: The close on this day, or None.
        first_close_price: The close on the first day shown, or None.

    Returns:
        The percentage, or "-" when it cannot be worked out.
    """
    if close_price is None or first_close_price is None:
        return "-"
    if first_close_price == 0:
        return "-"

    percentage = (float(close_price) / float(first_close_price)) * 100
    return f"{percentage:,.0f}%"


def days_between(earlier: date, later: date) -> int:
    """Count whole days from one date to another.

    Args:
        earlier: The starting date.
        later: The ending date.

    Returns:
        The number of days, negative if later precedes earlier.
    """
    difference = later - earlier
    return difference.days


def print_header(
    underlying: str,
    expiry_date_text: str,
    strike: float,
    option_type: str,
    identifier: str,
    shown_bar_count: int,
    total_bar_count: int,
    expiry_was_listed: bool,
) -> None:
    """Print the table header, including the traded-prices warning.

    Args:
        underlying: Underlying symbol.
        expiry_date_text: Expiry as "YYYY-MM-DD".
        strike: Strike price.
        option_type: "CALL" or "PUT".
        identifier: The contract identifier.
        shown_bar_count: How many daily bars are printed.
        total_bar_count: How many exist over the contract's whole life.
        expiry_was_listed: Whether the expiry is still in Tiger's current list.
    """
    print("=" * RULE_WIDTH)
    print(f"  {underlying} {expiry_date_text} {strike:,.2f} {option_type}")
    print(f"  Contract : {identifier.strip()}")
    if shown_bar_count == total_bar_count:
        print(f"  {total_bar_count} daily bars -- the contract's whole traded life")
    else:
        print(f"  last {shown_bar_count} of {total_bar_count} daily bars")
    if not expiry_was_listed:
        print("  EXPIRED -- no longer listed for trading, so this expiry could not be")
        print("            checked against Tiger's expiration list. The history below is")
        print("            what proves the contract was real.")
    print("=" * RULE_WIDTH)
    print()
    print("  *** THESE ARE TRADED PRICES, NOT BID/ASK QUOTES. ***")
    print()
    print("  Every price below is a price at which someone actually traded.")
    print("  You cannot buy at a close price. To buy you pay the ask, and to")
    print("  sell you receive the bid, and on a thin option the gap between")
    print("  them is wide. This tool is for understanding how a premium moved,")
    print("  not for costing an order.")
    print()
    print("=" * RULE_WIDTH)


def print_bars_table(bars_frame, expiry_date: date) -> None:
    """Print the daily price history.

    Args:
        bars_frame: The DataFrame from fetch_daily_bars.
        expiry_date: The contract's expiry, for the days-to-expiry column.
    """
    print(
        f"{'DATE':<12}{'OPEN':>9}{'HIGH':>9}{'LOW':>9}{'CLOSE':>9}"
        f"{'VS 1ST':>9}{'VOLUME':>12}{'OI':>12}{'DTE':>6}"
    )
    print("-" * RULE_WIDTH)

    first_close_price = None

    for _index, row in bars_frame.iterrows():
        bar_time_ms = read_optional_number(row, "time")
        if bar_time_ms is None:
            continue

        bar_date = milliseconds_to_date(int(bar_time_ms))

        open_price = read_optional_number(row, "open")
        high_price = read_optional_number(row, "high")
        low_price = read_optional_number(row, "low")
        close_price = read_optional_number(row, "close")
        volume = read_optional_number(row, "volume")
        open_interest = read_optional_number(row, "open_interest")

        # The baseline is the first bar that actually has a close, so a leading
        # row with no trade does not make the whole column meaningless.
        if first_close_price is None and close_price is not None:
            first_close_price = close_price

        days_to_expiry = days_between(bar_date, expiry_date)

        print(
            f"{bar_date.isoformat():<12}"
            f"{format_price(open_price):>9}"
            f"{format_price(high_price):>9}"
            f"{format_price(low_price):>9}"
            f"{format_price(close_price):>9}"
            f"{format_relative(close_price, first_close_price):>9}"
            f"{format_count(volume):>12}"
            f"{format_count(open_interest):>12}"
            f"{days_to_expiry:>6}"
        )

    print("-" * RULE_WIDTH)


def print_legend() -> None:
    """Explain the columns."""
    print("  VS 1ST   close as a percentage of the first close shown. 100% is where")
    print("           this view starts. Watch it fall as DTE shrinks: that is time")
    print("           decay, and it accelerates towards expiry.")
    print("  OI       open interest: contracts held. Volume is contracts traded.")
    print("  DTE      days to expiry on that date.")
    print()


def parse_option_type(raw_value: str) -> str:
    """Validate and normalise the option type.

    Args:
        raw_value: What the user typed.

    Returns:
        "CALL" or "PUT".

    Raises:
        MarketDataError: If it is neither.
    """
    normalised = raw_value.strip().upper()
    if normalised not in VALID_OPTION_TYPES:
        raise MarketDataError(
            f"Option type must be CALL or PUT, not {raw_value!r}."
        )
    return normalised


def build_argument_parser() -> argparse.ArgumentParser:
    """Define the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Show the daily premium history of one option contract.",
    )
    parser.add_argument("underlying", help="Underlying symbol, e.g. AAPL")
    parser.add_argument("expiry", help="Expiry as YYYY-MM-DD, e.g. 2026-09-18")
    parser.add_argument("strike", type=float, help="Strike price, e.g. 320")
    parser.add_argument("option_type", help="CALL or PUT")
    parser.add_argument(
        "--tail",
        type=int,
        default=0,
        help=(
            "show only the last N days instead of the contract's whole life. "
            "This trims the display; the full history is always fetched."
        ),
    )
    parser.add_argument(
        "--grab",
        action="store_true",
        help=(
            "claim market data device access, taking it from whatever held it. "
            "Not needed for this tool; historical bars are free."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the viewer.

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

        underlying = arguments.underlying.upper()
        option_type = parse_option_type(arguments.option_type)

        quote_client = build_quote_client(settings, grab_permission=arguments.grab)

        expiry_was_listed = verify_expiry(quote_client, underlying, arguments.expiry)

        identifier = build_contract_identifier(
            underlying, arguments.expiry, arguments.strike, option_type
        )
        bars_frame = fetch_daily_bars(quote_client, identifier)

        total_bar_count = len(bars_frame)
        if arguments.tail > 0:
            bars_frame = bars_frame.tail(arguments.tail)

        expiry_date = datetime.strptime(arguments.expiry, "%Y-%m-%d").date()

        print_header(
            underlying,
            arguments.expiry,
            arguments.strike,
            option_type,
            identifier,
            len(bars_frame),
            total_bar_count,
            expiry_was_listed,
        )
        print_bars_table(bars_frame, expiry_date)
        print_legend()

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
    except MarketDataError as error:
        print()
        print("NO USABLE MARKET DATA")
        print("-" * RULE_WIDTH)
        print(error)
        print("-" * RULE_WIDTH)
        return 4
    except Exception as error:  # noqa: BLE001 - top-level handler, message first
        print()
        print("THE REQUEST FAILED")
        print("-" * RULE_WIDTH)
        print(f"{type(error).__name__}: {error}")
        if "permission denied" in str(error).lower():
            print()
            print("This is a market data entitlement, not a bug. Historical option")
            print("bars are normally free -- run scripts/00_check_capabilities.py")
            print("to see exactly what this account can reach.")
        print("-" * RULE_WIDTH)
        print("Re-run with --debug for the full traceback.")
        if arguments.debug:
            print()
            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
