"""Market data: the shapes a market answers in, and where a quote comes from.

    data.py       the shapes -- expiry, spot, recent trade -- and date maths
    bars.py       one minute of trading, as a Bar
    quotes.py     THE SEAM -- where bid and ask come from
    price_log.py  a held contract's price, recorded every couple of seconds

Nothing here calls a broker. Each market fetches in its own way and answers
in these shapes: the US one is backend/markets/us/.
"""

from __future__ import annotations

from .data import (
    MarketDataError,
    MARKET_TIMEZONE, OptionExpiry, today_in_market_timezone,
    milliseconds_to_date, parse_expiry_date, days_until_expiry,
    SpotPrice,
    MAX_RECENT_TRADE_AGE_SECONDS, RecentTrade,
    DEFAULT_LIQUIDITY_THRESHOLD, UnderlyingPrice, LastTrade, ContractQuote,
    calculate_spread, is_low_liquidity,
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
    "RecentTrade", "MAX_RECENT_TRADE_AGE_SECONDS",
    "MarketDataError", "MARKET_TIMEZONE", "OptionExpiry",
    "today_in_market_timezone", "milliseconds_to_date", "parse_expiry_date",
    "days_until_expiry", "DEFAULT_LIQUIDITY_THRESHOLD",
    "UnderlyingPrice", "LastTrade", "ContractQuote", "calculate_spread",
    "is_low_liquidity",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS", "DECIMAL_SLIP_HIGH_RATIO",
    "DECIMAL_SLIP_LOW_RATIO", "WIDE_SPREAD_FRACTION", "QuoteSource",
    "QuoteEntryError", "QuoteSnapshot", "BidSnapshot", "MarketDataProvider",
    "check_price_is_positive", "check_bid_below_ask",
    "is_spread_suspiciously_wide", "is_limit_outside_spread",
    "decimal_slip_ratio", "is_decimal_slip", "build_override_phrase",
    "build_market_data_provider",
    "SpotPrice",
]
