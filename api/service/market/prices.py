"""What something last traded at, and how wide the market is.

Under manual entry this file supplies the *reference* prices -- the underlying,
and the last traded close a typed limit is sanity-checked against. The bid and
ask you actually trade on come from `quotes.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas
from tigeropen.common.consts import Market

from ..core.broker import (
    DELAYED_STOCK_BRIEFS_LIMITER, OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER, STOCK_BRIEFS_LIMITER,
)
from .calendar import milliseconds_to_date, today_in_market_timezone
from .fields import (


    MarketDataError, _read_optional_float, _read_optional_int, _read_text
)

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
    open_interest: int | None
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
