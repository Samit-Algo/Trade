"""Indian market data from OpenAlgo: the index, an option's last trade, bars.

A QUOTE CARRIES NO TIMESTAMP. OpenAlgo's quote is a last traded price and
nothing says when it traded. So its age is taken from the session: during
NSE hours it is live; outside them it is the close being held, and it is
reported as old so the trade route refuses to price an order on it -- the
same refusal a stale US price gets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.market import MarketDataError, RecentTrade, SpotPrice
from backend.services.market.bars import Bar

from .openalgo import unwrap

#: Reported as the age of a price read outside the session. Past every
#: freshness limit, so nothing is ever priced on a closed market's print.
CLOSED_MARKET_AGE_SECONDS = 24 * 60 * 60


def session_is_open(session, moment: datetime | None = None) -> bool:
    """Whether the exchange is in its trading hours, on a weekday."""
    now = (moment or datetime.now(timezone.utc)).astimezone(session.timezone)
    return now.weekday() < 5 and session.opens <= now.time() < session.closes


def read_quote(client, symbol: str, exchange: str) -> dict:
    """One quote: ltp, bid, ask, volume and the rest.

    Raises:
        MarketDataError: When OpenAlgo has none, or the price is not positive.
    """
    data = unwrap(client.quotes(symbol=symbol, exchange=exchange), f"The quote for {symbol}")
    if not isinstance(data, dict) or not float(data.get("ltp") or 0) > 0:
        raise MarketDataError(f"OpenAlgo returned no usable price for {symbol}: {data!r}")
    return data


def spot_price(client, symbol: str, index_exchange: str, session) -> SpotPrice | None:
    """The index's current level, or None when it cannot be read.

    None is a normal outcome, as for the US: the caller types the price.
    """
    try:
        quote = read_quote(client, symbol.strip().upper(), index_exchange)
    except Exception:  # noqa: BLE001 -- a missing spot is not an error here
        return None
    age = 0.0 if session_is_open(session) else float(CLOSED_MARKET_AGE_SECONDS)
    return SpotPrice(
        symbol=symbol.strip().upper(), price=float(quote["ltp"]),
        age_seconds=age, source="openalgo",
    )


def recent_traded_price(client, identifier: str, exchange: str, session) -> RecentTrade | None:
    """The option's last traded price, from its quote, or None.

    During the session it is treated as trading now if the contract has any
    volume today; outside it, it is marked stale and carries no volume.
    """
    try:
        quote = read_quote(client, identifier, exchange)
    except Exception:  # noqa: BLE001 -- the route reports "no price"
        return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if session_is_open(session):
        return RecentTrade(
            price=float(quote["ltp"]), bar_time_ms=now_ms, age_seconds=0.0,
            volume=int(quote.get("volume") or 0),
        )
    return RecentTrade(
        price=float(quote["ltp"]),
        bar_time_ms=now_ms - CLOSED_MARKET_AGE_SECONDS * 1000,
        age_seconds=float(CLOSED_MARKET_AGE_SECONDS),
        volume=0,
    )


def minute_bars(client, identifier: str, exchange: str, begin: datetime, end: datetime) -> list[Bar]:
    """One-minute bars over a window. Never raises: a missing chart is not a fault."""
    if begin >= end:
        return []
    ist = timezone(timedelta(hours=5, minutes=30))
    try:
        frame = client.history(
            symbol=identifier, exchange=exchange, interval="1m",
            start_date=begin.astimezone(ist).date().isoformat(),
            end_date=end.astimezone(ist).date().isoformat(),
        )
    except Exception:  # noqa: BLE001
        return []
    if frame is None or isinstance(frame, dict) or getattr(frame, "empty", True):
        return []

    bars = []
    begin_ms = begin.timestamp() * 1000
    end_ms = end.timestamp() * 1000
    for stamp, row in frame.iterrows():
        try:
            moment = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ist)
            time_ms = int(moment.timestamp() * 1000)
            bar = Bar(
                time_ms=time_ms, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=int(row.get("volume") or 0),
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if begin_ms - 60_000 <= bar.time_ms <= end_ms:
            bars.append(bar)
    return sorted(bars, key=lambda b: b.time_ms)
