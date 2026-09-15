"""Market data: what exists, what it last traded at, what it is worth now.

Four concerns, in dependency order:

    Reading             Tiger dataframes, without crashing on a missing column
    Calendar            what expiries exist, and the date maths
    Spot                the UNDERLYING share price, from Yahoo -- this account
                        has no usStockQuote entitlement, so Tiger is ~15min
                        stale, which is wide enough to pick a different strike
    Prices              last traded price, spread, liquidity

quotes.py stays a separate file on purpose: it is THE SEAM, the only place
allowed to name a concrete provider. Folding it in here would blur the one
boundary that keeps a market-data entitlement a one-line change.
"""

from __future__ import annotations

import pandas
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from tigeropen.common.consts import Market
from ..core.broker import EXPIRATIONS_LIMITER
import json
import time
import urllib.parse
import urllib.request
from ..core.broker import (
    DELAYED_STOCK_BRIEFS_LIMITER, OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER, STOCK_BRIEFS_LIMITER,
)


# --------------------------------------------------------------------------
# READ_DATA
# --------------------------------------------------------------------------

class MarketDataError(Exception):
    """Tiger returned nothing usable for a market data request."""

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


# --------------------------------------------------------------------------
# CALENDAR
# --------------------------------------------------------------------------

#: US options trade on US Eastern time. Days-to-expiry must be counted on the
#: market's calendar, not on the clock of whoever is running this (IST here).
MARKET_TIMEZONE = ZoneInfo("US/Eastern")


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


# --------------------------------------------------------------------------
# SPOT
# --------------------------------------------------------------------------

#: Yahoo serves the same chart API from two hosts. If one refuses, the other
#: usually answers, so a single bad host does not look like an outage.
QUOTE_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)

#: The endpoint answers with no User-Agent set, but not reliably. A browser
#: string is what its own web client sends.
USER_AGENT = "Mozilla/5.0"

#: Short on purpose. This sits in front of a human pressing a button, and a
#: slow answer is worse than no answer -- they can always type the price.
REQUEST_TIMEOUT_SECONDS = 6

#: Past this the price is stale enough to say so. Measured live, the feed
#: returns a stamp 1-3 seconds old during the session; outside trading hours
#: it holds the last trade, which can be many hours old.
LIVE_WITHIN_SECONDS = 90


@dataclass(frozen=True)
class SpotPrice:
    """The underlying's share price, and how much to trust it."""

    symbol: str
    price: float
    age_seconds: float
    source: str = "yahoo"

    @property
    def is_live(self) -> bool:
        """True when this is a currently-trading price, not a held close."""
        return self.age_seconds <= LIVE_WITHIN_SECONDS


def _read_chart_meta(host: str, symbol: str) -> dict | None:
    """Fetch one host's chart metadata for a symbol.

    Args:
        host: A base URL from QUOTE_HOSTS.
        symbol: The underlying, e.g. "AAPL".

    Returns:
        The `meta` block, or None for any failure at all.
    """
    url = (
        f"{host}/v8/finance/chart/{urllib.parse.quote(symbol)}"
        "?interval=1m&range=1d"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 -- every failure is the same failure here
        return None

    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None

    meta = result.get("meta")
    return meta if isinstance(meta, dict) else None


def fetch_spot_price(symbol: str) -> SpotPrice | None:
    """Fetch the underlying's live share price.

    Args:
        symbol: The underlying, e.g. "AAPL".

    Returns:
        The price with its age, or None when no host could answer. None is a
        normal outcome, not an error: the caller types the price instead.
    """
    cleaned = symbol.strip().upper()
    if not cleaned:
        return None

    for host in QUOTE_HOSTS:
        meta = _read_chart_meta(host, cleaned)
        if meta is None:
            continue

        price = meta.get("regularMarketPrice")
        stamped_at = meta.get("regularMarketTime")

        # A price without a timestamp cannot be judged for freshness, and an
        # unjudgeable price is exactly the kind this module exists to avoid.
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        if not isinstance(stamped_at, (int, float)):
            continue

        return SpotPrice(
            symbol=cleaned,
            price=round(float(price), 2),
            age_seconds=round(max(0.0, time.time() - float(stamped_at)), 1),
        )

    return None


# --------------------------------------------------------------------------
# PRICES
# --------------------------------------------------------------------------

#: A row with fewer than this many contracts traded, or this much open interest,
#: is thin. Thin contracts have wide spreads and can be hard to sell later.
DEFAULT_LIQUIDITY_THRESHOLD = 10


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
    historical_volatility: str | None  # NOT implied volatility
    spread: float | None
    spread_percent: float | None


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
    threshold: int = DEFAULT_LIQUIDITY_THRESHOLD,
) -> bool:
    """Decide whether a contract is too thinly traded to be comfortable.

    Volume is how many contracts changed hands today. A low number means few
    people are trading this contract, so the spread will be wide and selling
    later may be hard.

    Args:
        volume: Contracts traded today, or None if not reported.
        threshold: The number below which a figure counts as thin.

    Returns:
        True when the contract looks thin.
    """
    # A missing figure counts as thin. Absence of evidence is not reassurance
    # when the downside is being stuck in a position you cannot sell.
    if volume is None:
        return True

    return volume < threshold


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
    fetched-quote provider that quotes.py is waiting for.

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


@dataclass(frozen=True)
class RecentTrade:
    """The most recent price an option actually traded at, and how old it is.

    Not a quote. There is no bid and no ask here, because the endpoint that
    carries those needs the usOptionQuote entitlement this account does not
    have. What this IS: the last price somebody paid, usually seconds old
    while the market is open.
    """

    price: float
    bar_time_ms: int
    age_seconds: float
    volume: int | None

    @property
    def is_fresh(self) -> bool:
        """True when the price is recent enough to place an order against."""
        return self.age_seconds <= MAX_RECENT_TRADE_AGE_SECONDS

    @property
    def traded_this_minute(self) -> bool:
        """True when the newest bar is the minute we are in now.

        `age_seconds` measures time since the bar's minute BEGAN, not since
        the last trade. A bar labelled 10:06 read at 10:06:58 has an age of
        58s while its close is updating every couple of seconds. Measured
        live: volume went 518 -> 637 across twenty seconds inside one bar.

        So this is the real freshness question, and the one the data can
        actually answer: did this contract trade during the current minute?
        """
        now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
        return int(self.bar_time_ms // 60000) == int(now_ms // 60000)

    @property
    def is_live(self) -> bool:
        """True when the contract is trading right now, not carrying forward.

        Both halves matter. A bar for the current minute with zero volume is
        a placeholder, and its close is the last trade from some earlier
        minute -- exactly the thin-contract case where the price sat
        unchanged for 33 seconds.
        """
        return self.traded_this_minute and bool(self.volume)


#: Older than this and the price is not worth trading on. Measured live on
#: 2026-09-08: six AAPL contracts across the ladder all came back 18-19s old
#: during the session, so anything past a few minutes means the contract has
#: simply stopped trading -- or the market has closed.
MAX_RECENT_TRADE_AGE_SECONDS = 300


def fetch_recent_traded_price(quote_client, identifier: str) -> RecentTrade | None:
    """Fetch the newest traded price for one contract, from one-minute bars.

    FREE -- get_option_bars needs no market data entitlement, unlike
    get_option_briefs which would give a real bid and ask.

    The bar is bucketed by minute but its `close` updates as trades arrive, so
    polling this returns a price that is seconds old rather than a minute old.
    Verified live: the 09:34 bar read 6.43 and then 6.35 within the same
    minute.

    Do not ask for a period finer than one minute. Tiger ACCEPTS '1sec' and
    silently returns DAILY bars instead of refusing -- a wrong answer with a
    200 status, which is worse than an error.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: A full option identifier.

    Returns:
        The most recent trade, or None when the contract has never traded or
        the request failed. None is a legitimate answer, not an error.
    """
    from tigeropen.common.consts import BarPeriod, Market

    OPTION_BARS_LIMITER.wait()

    try:
        bars = quote_client.get_option_bars(
            identifiers=[identifier], period=BarPeriod.ONE_MINUTE, market=Market.US
        )
    except Exception:
        return None

    if bars is None or isinstance(bars, list) or bars.empty:
        return None
    if "time" not in bars.columns or "close" not in bars.columns:
        return None

    ordered = bars.sort_values("time", ascending=True)

    # The newest bar is often EMPTY -- Tiger opens a bar for the current
    # minute the moment it begins, carrying the previous close forward with
    # volume 0. Reading that reports a price nobody traded at, and it lags
    # whatever the broker's own app shows.
    #
    # Measured live:
    #     00:29  c=5.36  vol=21   <- the real last trade
    #     00:30  c=5.36  vol=0    <- carried forward, nothing happened
    #
    # So walk back to the newest bar that actually TRADED. A bar with no
    # volume holds no information that the one before it does not.
    newest = ordered.iloc[-1]
    for index in range(len(ordered) - 1, -1, -1):
        candidate = ordered.iloc[index]
        volume = _read_optional_int(candidate, "volume")
        if volume is None or volume > 0:
            newest = candidate
            break

    price = _read_optional_float(newest, "close")
    bar_time_ms = _read_optional_int(newest, "time")
    if price is None or bar_time_ms is None or price <= 0:
        return None

    now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
    return RecentTrade(
        price=price,
        bar_time_ms=bar_time_ms,
        age_seconds=round((now_ms - bar_time_ms) / 1000.0, 1),
        volume=_read_optional_int(newest, "volume"),
    )
