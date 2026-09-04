"""Option market data: expiration dates, chains, and quotes.

Read-only. Nothing here places an order or spends money.

Two rules this module exists to enforce:

1. Expiry dates are never constructed. Options only exist on the dates the
   exchange actually lists, and those dates are irregular -- weeklies, monthlies,
   quarterlies. A date you invent will look plausible and simply not exist, and
   you find out only when an order is rejected. Every date here comes from
   get_option_expirations.

2. Tiger sends times as milliseconds since 1970. Python's datetime works in
   seconds. Converting in one place stops that error spreading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas
from tigeropen.common.consts import Market

from .throttle import (
    CHAIN_LIMITER,
    DELAYED_STOCK_BRIEFS_LIMITER,
    EXPIRATIONS_LIMITER,
    OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER,
    STOCK_BRIEFS_LIMITER,
)

#: US options trade on US Eastern time. Days-to-expiry must be counted on the
#: market's calendar, not on the clock of whoever is running this (IST here).
MARKET_TIMEZONE = ZoneInfo("US/Eastern")

#: A row with fewer than this many contracts traded, or this much open interest,
#: is thin. Thin contracts have wide spreads and can be hard to sell later.
DEFAULT_LIQUIDITY_THRESHOLD = 10


class MarketDataError(Exception):
    """Tiger returned nothing usable for a market data request."""


# ---------------------------------------------------------------------------
# Data shapes
#
# The script that prints tables works with these, never with pandas. Keeping
# pandas inside this module means there is exactly one place to look when a
# column name changes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionExpiry:
    """One expiration date that Tiger says exists for an underlying."""

    date_text: str  # "YYYY-MM-DD", exactly as Tiger returned it
    expiry_date: date
    timestamp_ms: int
    period_tag: str  # "m" for monthly, "w" for weekly, "" if not reported
    days_to_expiry: int
    option_symbol: str  # Usually the underlying; index options can differ

    @property
    def period_label(self) -> str:
        """Return a readable version of the monthly/weekly tag."""
        if self.period_tag == "m":
            return "monthly"
        if self.period_tag == "w":
            return "weekly"
        return "unknown"


@dataclass(frozen=True)
class OptionRow:
    """One contract in an option chain, with liquidity already worked out."""

    identifier: str
    strike: float
    put_call: str  # "CALL" or "PUT"
    multiplier: int | None
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None
    implied_volatility: float | None  # from the chain's implied_vol column
    spread: float | None
    spread_percent: float | None
    is_thin: bool


@dataclass(frozen=True)
class StrikeRow:
    """The call and the put at one strike, side by side.

    Either side can be None: an exchange does not always list both.
    """

    strike: float
    call: OptionRow | None
    put: OptionRow | None


@dataclass(frozen=True)
class UnderlyingPrice:
    """The current price of the stock the options are written on."""

    symbol: str
    price: float
    is_delayed: bool  # True when it came from the free ~15-minute-delayed feed

    @property
    def freshness_label(self) -> str:
        """Return a readable note about how current this price is."""
        if self.is_delayed:
            return "delayed ~15 min"
        return "real-time"


@dataclass(frozen=True)
class LastTrade:
    """The most recent day on which a contract actually traded."""

    close: float
    trade_date: date
    days_old: int


@dataclass(frozen=True)
class ContractQuote:
    """A live quote for one specific contract.

    Deliberately kept separate from OptionRow. The chain endpoint returns
    IMPLIED volatility; this endpoint returns HISTORICAL volatility. They are
    different numbers and must not share a field name.
    """

    identifier: str
    symbol: str
    strike: float | None
    put_call: str
    multiplier: int | None
    bid: float | None
    ask: float | None
    latest_price: float | None
    volume: int | None
    open_interest: int | None
    historical_volatility: str | None  # NOT implied volatility
    spread: float | None
    spread_percent: float | None


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------


def today_in_market_timezone() -> date:
    """Return today's date on the US market's clock.

    Returns:
        The current date in US/Eastern.
    """
    now_in_new_york = datetime.now(MARKET_TIMEZONE)
    return now_in_new_york.date()


def milliseconds_to_date(milliseconds: int) -> date:
    """Convert one of Tiger's millisecond timestamps to a calendar date.

    Args:
        milliseconds: Milliseconds since 1970-01-01 UTC, as Tiger sends them.

    Returns:
        The corresponding date in US/Eastern.
    """
    # Tiger sends milliseconds; Python's fromtimestamp expects seconds.
    seconds = milliseconds / 1000

    moment_in_utc = datetime.fromtimestamp(seconds, tz=timezone.utc)

    # Expiry timestamps are midnight US/Eastern. Reading them in any other
    # zone can land on the previous or next day and shift the whole expiry.
    moment_in_new_york = moment_in_utc.astimezone(MARKET_TIMEZONE)
    return moment_in_new_york.date()


def parse_expiry_date(date_text: str) -> date:
    """Turn Tiger's "YYYY-MM-DD" expiry string into a date object.

    Args:
        date_text: An expiry date string exactly as the API returned it.

    Returns:
        The parsed date.

    Raises:
        MarketDataError: If the text is not in the expected format.
    """
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as error:
        raise MarketDataError(
            f"Could not read {date_text!r} as an expiry date (expected YYYY-MM-DD)."
        ) from error
    return parsed.date()


def days_until_expiry(expiry_date: date) -> int:
    """Count whole days from today to an expiry date.

    Args:
        expiry_date: The date the option expires.

    Returns:
        Days remaining. 0 means it expires today; a negative number means the
        date has already passed.
    """
    today = today_in_market_timezone()
    difference = expiry_date - today
    return difference.days


# ---------------------------------------------------------------------------
# Pure calculations. No network, no pandas -- easy to read and to test.
# ---------------------------------------------------------------------------


def calculate_spread(
    bid: float | None,
    ask: float | None,
) -> tuple[float | None, float | None]:
    """Work out the gap between the bid and the ask.

    The spread is what it costs you to be wrong immediately. If you buy at the
    ask and change your mind, you sell at the bid, and the difference is gone
    before the price has moved at all. On a thin option that can be several
    percent of the premium, which is why it is shown on every row.

    Args:
        bid: Highest price a buyer is currently offering, or None.
        ask: Lowest price a seller is currently asking, or None.

    Returns:
        A pair of (spread in dollars, spread as a percentage of the ask).
        Either entry is None when it cannot be worked out.
    """
    if bid is None or ask is None:
        return None, None

    spread = ask - bid

    # Dividing by the ask needs a non-zero ask. An ask of 0 means nobody is
    # offering the contract at all, so a percentage would be meaningless.
    if ask <= 0:
        return spread, None

    spread_percent = (spread / ask) * 100
    return spread, spread_percent


def is_low_liquidity(
    volume: int | None,
    open_interest: int | None,
    threshold: int = DEFAULT_LIQUIDITY_THRESHOLD,
) -> bool:
    """Decide whether a contract is too thinly traded to be comfortable.

    Volume is how many contracts changed hands today. Open interest is how many
    are held in total. Low numbers on either mean few people are trading this
    contract, so the spread will be wide and selling later may be hard.

    Args:
        volume: Contracts traded today, or None if not reported.
        open_interest: Contracts currently held, or None if not reported.
        threshold: The number below which a figure counts as thin.

    Returns:
        True when the contract looks thin.
    """
    # A missing figure counts as thin. Absence of evidence is not reassurance
    # when the downside is being stuck in a position you cannot sell.
    if volume is None or open_interest is None:
        return True

    if volume < threshold:
        return True
    if open_interest < threshold:
        return True
    return False


def find_atm_strike(
    strike_rows: list[StrikeRow],
    underlying_price: float,
) -> float | None:
    """Find the listed strike closest to the current share price.

    "At the money" is the strike nearest the spot price. It is the dividing
    line: calls below it and puts above it are in the money and already hold
    real value; the rest are out of the money and are time value alone.

    Args:
        strike_rows: Rows to search.
        underlying_price: Current price of the underlying share.

    Returns:
        The closest strike, or None if there are no rows.
    """
    if not strike_rows:
        return None

    closest_strike = strike_rows[0].strike
    smallest_distance = abs(strike_rows[0].strike - underlying_price)

    for strike_row in strike_rows:
        distance = abs(strike_row.strike - underlying_price)
        if distance < smallest_distance:
            smallest_distance = distance
            closest_strike = strike_row.strike

    return closest_strike


def select_strikes_around_price(
    strike_rows: list[StrikeRow],
    underlying_price: float,
    count_each_side: int,
) -> list[StrikeRow]:
    """Take a window of strikes centred on the current share price.

    A full chain can run to hundreds of strikes, most of them far from the
    money and of no interest. This keeps the rows either side of the spot price.

    Args:
        strike_rows: Rows sorted by strike, ascending.
        underlying_price: Current price of the underlying share.
        count_each_side: How many strikes to keep above and below the middle.

    Returns:
        The selected rows, still sorted ascending. Returns everything when the
        chain is already shorter than the window.
    """
    if not strike_rows:
        return []

    atm_strike = find_atm_strike(strike_rows, underlying_price)

    middle_index = 0
    for index, strike_row in enumerate(strike_rows):
        if strike_row.strike == atm_strike:
            middle_index = index
            break

    first_index = middle_index - count_each_side
    if first_index < 0:
        first_index = 0

    last_index = middle_index + count_each_side + 1

    return strike_rows[first_index:last_index]


def pair_calls_and_puts_by_strike(option_rows: list[OptionRow]) -> list[StrikeRow]:
    """Group a flat list of contracts into one row per strike.

    A chain arrives flat: every call, then every put. A human reads a chain as
    CALLS | STRIKE | PUTS, with the call and the put at each strike on the same
    line. This does that regrouping.

    Args:
        option_rows: Contracts for one underlying and one expiry.

    Returns:
        One StrikeRow per strike, sorted by strike ascending so the table always
        reads the same way.
    """
    calls_by_strike: dict[float, OptionRow] = {}
    puts_by_strike: dict[float, OptionRow] = {}
    every_strike: set[float] = set()

    for option_row in option_rows:
        every_strike.add(option_row.strike)
        if option_row.put_call == "CALL":
            calls_by_strike[option_row.strike] = option_row
        elif option_row.put_call == "PUT":
            puts_by_strike[option_row.strike] = option_row

    strike_rows = []
    for strike in sorted(every_strike):
        call_at_this_strike = calls_by_strike.get(strike)
        put_at_this_strike = puts_by_strike.get(strike)
        strike_rows.append(
            StrikeRow(
                strike=strike,
                call=call_at_this_strike,
                put=put_at_this_strike,
            )
        )

    return strike_rows


# ---------------------------------------------------------------------------
# Reading values out of a pandas DataFrame
#
# The SDK returns DataFrames: a table where each row is a record and each
# column has a name. `for index, row in frame.iterrows()` walks it one row at
# a time, and `row["volume"]` reads one named column of that row.
#
# Two things to watch for, which is why the helpers below exist:
#   - A column may be missing entirely from a response.
#   - A cell may hold NaN ("not a number"), pandas' way of writing "no value".
#     NaN is a float, so it passes an `is None` check and then poisons any
#     arithmetic it touches. pandas.isna() is the correct test.
# ---------------------------------------------------------------------------


def _read_optional_float(row: pandas.Series, column_name: str) -> float | None:
    """Read one column of a DataFrame row as a float, or None if unusable.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.

    Returns:
        The value as a float, or None if the column is absent or empty.
    """
    if column_name not in row:
        return None

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return None

    return float(raw_value)


def _read_optional_int(row: pandas.Series, column_name: str) -> int | None:
    """Read one column of a DataFrame row as an integer, or None if unusable.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.

    Returns:
        The value as an int, or None if the column is absent or empty.
    """
    if column_name not in row:
        return None

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return None

    return int(raw_value)


def _read_text(row: pandas.Series, column_name: str, default: str = "") -> str:
    """Read one column of a DataFrame row as text.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.
        default: What to return when the column is absent or empty.

    Returns:
        The value as a string, or the default.
    """
    if column_name not in row:
        return default

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return default

    return str(raw_value)


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------


def list_expirations(quote_client, underlying: str) -> list[OptionExpiry]:
    """List every expiration date Tiger reports for an underlying.

    This is the only source of expiry dates in the project. Nothing anywhere
    builds a date from a calendar rule, because listed expiries are irregular
    and a constructed date that does not exist fails only at order time.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol, for example "AAPL".

    Returns:
        Expiries sorted by date, soonest first.

    Raises:
        MarketDataError: If Tiger returns no expirations for the symbol.
    """
    EXPIRATIONS_LIMITER.wait()

    # market is passed explicitly rather than relying on the server default,
    # so a symbol that could be read as non-US cannot silently change market.
    expirations_frame = quote_client.get_option_expirations(
        symbols=[underlying],
        market=Market.US,
    )

    if expirations_frame is None or expirations_frame.empty:
        raise MarketDataError(
            f"Tiger returned no option expirations for {underlying!r}. "
            "Check the symbol, and that it has listed options."
        )

    expiries = []
    for _index, row in expirations_frame.iterrows():
        date_text = _read_text(row, "date")
        if not date_text:
            continue

        expiry_date = parse_expiry_date(date_text)
        timestamp_ms = _read_optional_int(row, "timestamp")
        period_tag = _read_text(row, "period_tag")
        option_symbol = _read_text(row, "option_symbol", default=underlying)
        days_remaining = days_until_expiry(expiry_date)

        expiries.append(
            OptionExpiry(
                date_text=date_text,
                expiry_date=expiry_date,
                timestamp_ms=timestamp_ms if timestamp_ms is not None else 0,
                period_tag=period_tag,
                days_to_expiry=days_remaining,
                option_symbol=option_symbol,
            )
        )

    if not expiries:
        raise MarketDataError(
            f"Tiger returned expiration rows for {underlying!r} but none had a date."
        )

    expiries.sort(key=lambda expiry: expiry.expiry_date)
    return expiries


def fetch_option_chain(quote_client, underlying: str, expiry_date_text: str):
    """Fetch the raw option chain for one underlying and one expiry.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol, for example "AAPL".
        expiry_date_text: An expiry in "YYYY-MM-DD" form. This must be a date
            that came from list_expirations, never one you assembled yourself.

    Returns:
        The pandas DataFrame the SDK returned.

    Raises:
        MarketDataError: If the chain comes back empty.
    """
    CHAIN_LIMITER.wait()

    # return_greek_value is deliberately not passed. The chain's Greeks are
    # deprecated by Tiger, update only once a day, and must not be used for
    # intraday decisions, so this project does not request or display them.
    chain_frame = quote_client.get_option_chain(
        symbol=underlying,
        expiry=expiry_date_text,
        market=Market.US,
    )

    if chain_frame is None or chain_frame.empty:
        raise MarketDataError(
            f"Tiger returned an empty option chain for {underlying} {expiry_date_text}."
        )

    return chain_frame


def build_option_rows(
    chain_frame,
    liquidity_threshold: int = DEFAULT_LIQUIDITY_THRESHOLD,
) -> list[OptionRow]:
    """Turn a chain DataFrame into plain OptionRow objects.

    This is the only place the chain's column names are used, so a change in
    the API means editing one function.

    Args:
        chain_frame: The DataFrame from fetch_option_chain.
        liquidity_threshold: Passed through to is_low_liquidity.

    Returns:
        One OptionRow per contract, with spread and liquidity already computed.
    """
    option_rows = []

    for _index, row in chain_frame.iterrows():
        identifier = _read_text(row, "identifier")
        strike = _read_optional_float(row, "strike")
        put_call = _read_text(row, "put_call").upper()

        # A row with no strike or no side cannot be placed in a chain table.
        if strike is None or not put_call:
            continue

        bid = _read_optional_float(row, "bid_price")
        ask = _read_optional_float(row, "ask_price")
        volume = _read_optional_int(row, "volume")
        open_interest = _read_optional_int(row, "open_interest")
        multiplier = _read_optional_int(row, "multiplier")

        # IMPLIED volatility. The option-chain endpoint spells this column
        # "implied_vol". Do not confuse it with the "volatility" column of
        # get_option_briefs, which is HISTORICAL volatility -- a different
        # measure entirely. See ContractQuote.historical_volatility.
        implied_volatility = _read_optional_float(row, "implied_vol")

        spread, spread_percent = calculate_spread(bid, ask)
        thin = is_low_liquidity(volume, open_interest, liquidity_threshold)

        option_rows.append(
            OptionRow(
                identifier=identifier,
                strike=strike,
                put_call=put_call,
                multiplier=multiplier,
                bid=bid,
                ask=ask,
                volume=volume,
                open_interest=open_interest,
                implied_volatility=implied_volatility,
                spread=spread,
                spread_percent=spread_percent,
                is_thin=thin,
            )
        )

    return option_rows


def fetch_underlying_price(quote_client, underlying: str) -> UnderlyingPrice:
    """Fetch the current share price of the underlying.

    A chain without a spot price cannot be read: you cannot see which strikes
    are in the money, or which row is at the money.

    Real-time quotes require purchased market data access. When that is not
    available this falls back to Tiger's free delayed feed, which lags by
    roughly 15 minutes, and says so in the returned object.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol, for example "AAPL".

    Returns:
        The price, marked as real-time or delayed.

    Raises:
        MarketDataError: If neither feed returns a usable price.
    """
    realtime_price = _try_fetch_realtime_price(quote_client, underlying)
    if realtime_price is not None:
        return UnderlyingPrice(
            symbol=underlying,
            price=realtime_price,
            is_delayed=False,
        )

    delayed_price = _try_fetch_delayed_price(quote_client, underlying)
    if delayed_price is not None:
        return UnderlyingPrice(
            symbol=underlying,
            price=delayed_price,
            is_delayed=True,
        )

    raise MarketDataError(
        f"Could not get a price for {underlying!r} from either the real-time or "
        "the delayed feed. Real-time quotes need purchased market data access."
    )


def _try_fetch_realtime_price(quote_client, underlying: str) -> float | None:
    """Try the real-time stock quote endpoint.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol.

    Returns:
        The latest price, or None if the endpoint is unavailable or empty.
    """
    STOCK_BRIEFS_LIMITER.wait()

    # get_stock_briefs takes no market parameter, unlike the option endpoints.
    try:
        briefs_frame = quote_client.get_stock_briefs(symbols=[underlying])
    except Exception:
        # Most often this means market data access has not been purchased.
        # That is not a bug, so we fall back rather than fail.
        return None

    if briefs_frame is None or briefs_frame.empty:
        return None

    first_row = briefs_frame.iloc[0]
    return _read_optional_float(first_row, "latest_price")


def _try_fetch_delayed_price(quote_client, underlying: str) -> float | None:
    """Try Tiger's free delayed stock quote endpoint.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol.

    Returns:
        The delayed closing price, or None if unavailable.
    """
    DELAYED_STOCK_BRIEFS_LIMITER.wait()

    try:
        delayed_frame = quote_client.get_stock_delay_briefs(symbols=[underlying])
    except Exception:
        return None

    if delayed_frame is None or delayed_frame.empty:
        return None

    first_row = delayed_frame.iloc[0]

    # The delayed endpoint has no "latest_price" column. It reports "close",
    # which during market hours is the most recent delayed print.
    return _read_optional_float(first_row, "close")


def fetch_contract_quote(quote_client, identifier: str) -> ContractQuote:
    """Fetch a live quote for one specific option contract.

    DORMANT, not dead. A Phase 2 deliverable that nothing calls yet because
    get_option_briefs needs the `usOptionQuote` entitlement, which has not been
    bought. Buying it activates this, and this is the natural body of the
    TigerQuoteProvider that providers.py is waiting for.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: A full option identifier, e.g. "AAPL  260918C00320000".

    Returns:
        The quote for that contract.

    Raises:
        MarketDataError: If Tiger returns nothing for the identifier.
    """
    OPTION_BRIEFS_LIMITER.wait()

    briefs_frame = quote_client.get_option_briefs(
        identifiers=[identifier],
        market=Market.US,
    )

    if briefs_frame is None or briefs_frame.empty:
        raise MarketDataError(f"Tiger returned no quote for {identifier!r}.")

    row = briefs_frame.iloc[0]

    bid = _read_optional_float(row, "bid_price")
    ask = _read_optional_float(row, "ask_price")
    spread, spread_percent = calculate_spread(bid, ask)

    # HISTORICAL volatility, not implied. This endpoint's column is called
    # "volatility" and describes how much the underlying has actually moved in
    # the past. The option chain's "implied_vol" is what the market expects
    # ahead. Mixing them up gives a number that looks right and is not.
    historical_volatility = _read_text(row, "volatility") or None

    return ContractQuote(
        identifier=_read_text(row, "identifier", default=identifier),
        symbol=_read_text(row, "symbol"),
        strike=_read_optional_float(row, "strike"),
        put_call=_read_text(row, "put_call").upper(),
        multiplier=_read_optional_int(row, "multiplier"),
        bid=bid,
        ask=ask,
        latest_price=_read_optional_float(row, "latest_price"),
        volume=_read_optional_int(row, "volume"),
        open_interest=_read_optional_int(row, "open_interest"),
        historical_volatility=historical_volatility,
        spread=spread,
        spread_percent=spread_percent,
    )


def fetch_last_traded_close(quote_client, identifier: str) -> LastTrade | None:
    """Fetch the most recent daily close for one contract.

    Used as a sanity check against hand-typed prices. It is free: historical
    option bars need no quote entitlement, unlike the chain and briefs.

    This is a check, never a price source. On a thin contract the last trade
    may be days old, which is why days_old is returned alongside it.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: A full option identifier.

    Returns:
        The last traded close, or None when the contract has no history or the
        request fails. None is a legitimate answer, not an error: a contract
        that has never traded has no last price.
    """
    from tigeropen.common.consts import BarPeriod, Market

    OPTION_BARS_LIMITER.wait()

    try:
        bars_frame = quote_client.get_option_bars(
            identifiers=[identifier],
            period=BarPeriod.DAY,
            market=Market.US,
        )
    except Exception:
        return None

    # A contract with no history comes back as an empty *list*, not an empty
    # DataFrame, so testing .empty alone would raise AttributeError.
    if bars_frame is None or isinstance(bars_frame, list) or bars_frame.empty:
        return None

    if "time" not in bars_frame.columns or "close" not in bars_frame.columns:
        return None

    sorted_bars = bars_frame.sort_values("time", ascending=True)
    final_bar = sorted_bars.iloc[-1]

    close_price = _read_optional_float(final_bar, "close")
    bar_time_ms = _read_optional_int(final_bar, "time")

    if close_price is None or bar_time_ms is None:
        return None

    trade_date = milliseconds_to_date(bar_time_ms)
    days_old = (today_in_market_timezone() - trade_date).days

    return LastTrade(close=close_price, trade_date=trade_date, days_old=days_old)


def fetch_underlying_price_safely(quote_client, underlying: str) -> UnderlyingPrice | None:
    """Fetch the underlying price, tolerating its absence.

    A preview reads better with a spot price but does not depend on one, and
    this account may have no entitlement for the real-time feed. Returning None
    lets a caller carry on and say "price unavailable" rather than fail an
    order preview over a decoration.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol.

    Returns:
        The price, or None if it could not be read for any reason.
    """
    try:
        return fetch_underlying_price(quote_client, underlying)
    except Exception:
        return None
