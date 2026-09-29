"""Market data shapes, and the date maths. No broker is called from here.

    Errors      MarketDataError
    Calendar    an expiry, and counting the days to it
    Spot        the underlying's price, and how much to trust it
    Prices      last traded price, spread, liquidity

Each market fetches these in its own way -- the US one in
backend/markets/us/data.py -- and hands back the shapes defined here.

quotes.py stays a separate file on purpose: it is THE SEAM, the only place
allowed to name a concrete provider. Folding it in here would blur the one
boundary that keeps a market-data entitlement a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


class MarketDataError(Exception):
    """The broker returned nothing usable for a market data request."""


# --------------------------------------------------------------------------
# CALENDAR
# --------------------------------------------------------------------------

#: US options trade on US Eastern time. Days-to-expiry must be counted on the
#: market's calendar, not on the clock of whoever is running this (IST here).
MARKET_TIMEZONE = ZoneInfo("US/Eastern")


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


# --------------------------------------------------------------------------
# SPOT
# --------------------------------------------------------------------------


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

