"""The underlying's share price, live, from Yahoo Finance.

WHY THIS EXISTS. This account holds no `usStockQuote` entitlement, so Tiger's
own feed returns a price roughly 15 MINUTES stale. Measured on 2026-09-10
while the market was open:

    AAPL   Yahoo 319.71   Tiger 319.39   (+0.32)
    NVDA   Yahoo 217.90   Tiger 217.80   (+0.10)
    TSLA   Yahoo 367.42   Tiger 365.32   (+2.10)   <-- more than one strike

That TSLA gap is the whole argument. `current_price` chooses the STRIKE, and
$2.10 is enough to choose a different one. Yahoo's number was independently
confirmed against Nasdaq's own API (319.71) in the same minute, so it is Tiger
that is behind, not Yahoo that is wrong.

WHAT THIS IS NOT. An undocumented endpoint, not a contracted feed. It may
change shape, rate-limit, or vanish without notice. Every failure here is
therefore returned as None rather than raised: the UI falls back to what it
always did, which is a human typing the price from the Tiger app.

Two hosts are tried because Yahoo runs both and either can refuse alone.

NOTHING HERE IS USED AS A PRICE PAID. The option's own premium comes from
Tiger's free one-minute bars and is unaffected by any of this.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

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
