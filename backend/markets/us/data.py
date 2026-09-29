"""US market data: Tiger for options, Yahoo for the underlying's price.

    Reading     Tiger's DataFrames, without crashing on a missing column
    Calendar    what expiries Tiger lists
    Spot        the UNDERLYING share price, from Yahoo -- this account has no
                usStockQuote entitlement, so Tiger's own is ~15min stale,
                which is wide enough to pick a different strike
    Prices      the last traded price, and the contract quote

The shapes these return -- OptionExpiry, SpotPrice, RecentTrade and the rest
-- are neutral, and live in backend/services/market/data.py.

READING A DATAFRAME. The SDK returns DataFrames: a table where each row is a
record and each column has a name. Two things to watch for, which is why the
_read_* helpers exist: a column may be missing entirely from a response, and
a cell may hold NaN, pandas' way of writing "no value". NaN is a float, so it
passes an `is None` check and then poisons any arithmetic it touches.
pandas.isna() is the correct test.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

import pandas
from tigeropen.common.consts import Market

from backend.services.market.data import (
    ContractQuote,
    LastTrade,
    MarketDataError,
    OptionExpiry,
    RecentTrade,
    SpotPrice,
    UnderlyingPrice,
    calculate_spread,
    days_until_expiry,
    milliseconds_to_date,
    parse_expiry_date,
    today_in_market_timezone,
)

from .broker import (
    DELAYED_STOCK_BRIEFS_LIMITER,
    EXPIRATIONS_LIMITER,
    OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER,
    STOCK_BRIEFS_LIMITER,
)


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
