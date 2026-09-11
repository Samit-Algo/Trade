"""Market data: what exists, what it last traded at, what it is worth now.

    read_data.py    Reading Tiger's dataframes without crashing
    calendar.py  What expiries exist, and the date maths
    prices.py    Underlying price, last traded price, spread, liquidity
    quotes.py    THE SEAM -- where bid and ask come from

`quotes.py` is the only file allowed to name a concrete provider. Today the
only one is the manual-entry provider: you type the bid and the ask. When
the market-data entitlement is bought, a fetched-quote provider replaces it
and one line of `.env` changes -- nothing else needs touching, which stays
true only while no other file names either class.
"""

from __future__ import annotations

from .read_data import (
    MarketDataError
)
from .calendar import (
    MARKET_TIMEZONE, OptionExpiry, today_in_market_timezone,
    milliseconds_to_date, parse_expiry_date, days_until_expiry,
    list_expirations
)
from .spot import SpotPrice, fetch_spot_price
from .prices import (
    MAX_RECENT_TRADE_AGE_SECONDS, RecentTrade, fetch_recent_traded_price,
    DEFAULT_LIQUIDITY_THRESHOLD, UnderlyingPrice, LastTrade, ContractQuote,
    calculate_spread, is_low_liquidity, fetch_underlying_price,
    fetch_contract_quote, fetch_last_traded_close,
    fetch_underlying_price_safely
)
from .quotes import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS, DECIMAL_SLIP_HIGH_RATIO,
    DECIMAL_SLIP_LOW_RATIO, WIDE_SPREAD_FRACTION, QuoteSource,
    QuoteEntryError, QuoteSnapshot, BidSnapshot, MarketDataProvider,
    check_price_is_positive, check_bid_below_ask,
    is_spread_suspiciously_wide, is_limit_outside_spread,
    decimal_slip_ratio, is_decimal_slip, build_override_phrase,
    build_market_data_provider
)

__all__ = [
    "RecentTrade", "fetch_recent_traded_price",
    "MAX_RECENT_TRADE_AGE_SECONDS",
    "MarketDataError", "MARKET_TIMEZONE", "OptionExpiry",
    "today_in_market_timezone", "milliseconds_to_date", "parse_expiry_date",
    "days_until_expiry", "list_expirations", "DEFAULT_LIQUIDITY_THRESHOLD",
    "UnderlyingPrice", "LastTrade", "ContractQuote", "calculate_spread",
    "is_low_liquidity", "fetch_underlying_price", "fetch_contract_quote",
    "fetch_last_traded_close", "fetch_underlying_price_safely",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS", "DECIMAL_SLIP_HIGH_RATIO",
    "DECIMAL_SLIP_LOW_RATIO", "WIDE_SPREAD_FRACTION", "QuoteSource",
    "QuoteEntryError", "QuoteSnapshot", "BidSnapshot", "MarketDataProvider",
    "check_price_is_positive", "check_bid_below_ask",
    "is_spread_suspiciously_wide", "is_limit_outside_spread",
    "decimal_slip_ratio", "is_decimal_slip", "build_override_phrase",
    "build_market_data_provider",
    "SpotPrice", "fetch_spot_price",
]
