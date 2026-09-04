"""What the routes need, built once and shared.

The Tiger clients are expensive to construct -- each one reads and parses the
private key -- and the SDK's own docs recommend one module-level QuoteClient
reused rather than many. So they are built lazily on first use and cached.

Nothing here contains business logic. It hands routes the same objects the
CLI scripts build for themselves, so both entry points call the same library
functions with the same inputs.
"""

from __future__ import annotations

import threading

from tiger_backend.clients import build_quote_client, build_trade_client
from tiger_backend.config import Settings, load_settings
from tiger_backend.providers import MarketDataProvider, build_market_data_provider

from .tokens import PreviewTokenStore

# REENTRANT on purpose. get_quote_client() holds this lock and then calls
# get_settings(), which takes it again on the same thread. A plain Lock
# deadlocks there -- and it deadlocks on the first real request, not at
# import, so it looks like a hang rather than a crash.
_lock = threading.RLock()
_settings: Settings | None = None
_quote_client = None
_trade_client = None
_provider: MarketDataProvider | None = None
_token_store: PreviewTokenStore | None = None


def get_settings() -> Settings:
    """Return the validated settings, loading them once.

    Returns:
        The frozen Settings object.
    """
    global _settings
    with _lock:
        if _settings is None:
            _settings = load_settings()
        return _settings


def get_quote_client():
    """Return the shared QuoteClient.

    Built with grab_permission=False, exactly as the CLI does: claiming market
    data device access would take primary-device status away from whatever held
    it, such as the Tiger app on a phone. Nothing here needs it.

    Returns:
        A tigeropen QuoteClient.
    """
    global _quote_client
    with _lock:
        if _quote_client is None:
            _quote_client = build_quote_client(get_settings(), grab_permission=False)
        return _quote_client


def get_trade_client():
    """Return the shared TradeClient.

    Returns:
        A tigeropen TradeClient.
    """
    global _trade_client
    with _lock:
        if _trade_client is None:
            _trade_client = build_trade_client(get_settings())
        return _trade_client


def get_market_data_provider() -> MarketDataProvider:
    """Return the shared market data provider.

    Built through the same factory the CLI uses, so MARKET_DATA_SOURCE governs
    both entry points identically. Note that ManualEntryProvider's prompting
    methods are never called over HTTP -- the API validates a quote supplied in
    the request body instead. The provider is here so the decimal-slip check
    can reach the same last-traded-price lookup.

    Returns:
        A MarketDataProvider.
    """
    global _provider
    with _lock:
        if _provider is None:
            _provider = build_market_data_provider(
                get_settings(), quote_client=get_quote_client()
            )
        return _provider


def get_token_store() -> PreviewTokenStore:
    """Return the shared preview token store.

    Returns:
        The store, with its TTL taken from settings.
    """
    global _token_store
    with _lock:
        if _token_store is None:
            _token_store = PreviewTokenStore(
                ttl_seconds=get_settings().preview_token_ttl_seconds
            )
        return _token_store


def orders_are_enabled(settings: Settings) -> tuple[bool, str]:
    """Decide whether order endpoints may run at all, and say why not.

    This is a fast, clear refusal in front of the real guard, not a replacement
    for it. assert_order_allowed still runs twice inside orders.py on every
    order path; deleting this check would change the error a client sees, not
    whether an order could be placed.

    Args:
        settings: The validated settings.

    Returns:
        A pair of (enabled, reason when not enabled).
    """
    if settings.dry_run:
        return False, (
            "DRY_RUN is true, so no order can be submitted. This is Lock 3, and "
            "it is on by default. Set DRY_RUN=false in .env only when you intend "
            "to place real orders."
        )

    if settings.mode != "PAPER":
        return False, (
            f"The account resolved to {settings.mode}, not PAPER. This build "
            "refuses to submit non-paper orders."
        )

    return True, ""


def reset_for_testing() -> None:
    """Clear every cached singleton. Used by tests, never in production."""
    global _settings, _quote_client, _trade_client, _provider, _token_store
    with _lock:
        _settings = None
        _quote_client = None
        _trade_client = None
        _provider = None
        _token_store = None
