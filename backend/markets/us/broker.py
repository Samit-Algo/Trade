"""Talking to Tiger: the connection, and not talking too fast.

Two halves of one job -- everything about reaching the broker lives here.

  1. **Rate limits.** Tiger publishes a separate request limit per endpoint.
     Going over gets requests rejected, so every call in this project waits on
     a limiter first. The numbers are copied from the documentation and live
     here, in one place, so changing one is a single edit.
  2. **The clients.** QuoteClient (market data) and TradeClient (account and
     orders), built separately on purpose: market data is read-only and
     harmless, trading is not, and code holding one should not implicitly hold
     the other.

Every attribute set on the client config is a documented one:
https://docs-en.itigerup.com/docs/prepare
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.config import Settings
from backend.core.rate_limit import RateLimiter

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tigeropen.quote.quote_client import QuoteClient
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    from tigeropen.trade.trade_client import TradeClient


# --------------------------------------------------------------------------
# One limiter per endpoint. Each number is the documented base rate limit.
# --------------------------------------------------------------------------

#: https://docs-en.itigerup.com/docs/quote-option -- 60 requests/minute
EXPIRATIONS_LIMITER = RateLimiter(60, "get_option_expirations")

#: https://docs-en.itigerup.com/docs/quote-option -- 60 requests/minute
CHAIN_LIMITER = RateLimiter(60, "get_option_chain")

#: https://docs-en.itigerup.com/docs/quote-option -- 120 requests/minute
OPTION_BRIEFS_LIMITER = RateLimiter(120, "get_option_briefs")

#: https://docs-en.itigerup.com/docs/quote-stock -- 120 requests/minute
STOCK_BRIEFS_LIMITER = RateLimiter(120, "get_stock_briefs")

#: https://docs-en.itigerup.com/docs/quote-stock -- 10 requests/minute.
#: Much tighter than the real-time endpoint, so only used as a fallback.
DELAYED_STOCK_BRIEFS_LIMITER = RateLimiter(10, "get_stock_delay_briefs")

#: https://docs-en.itigerup.com/docs/quote-option -- no limit documented for
#: depth or timeline, so the tighter chain limit is used as a safe default.
OPTION_DEPTH_LIMITER = RateLimiter(60, "get_option_depth")
OPTION_TIMELINE_LIMITER = RateLimiter(60, "get_option_timeline")

#: https://docs-en.itigerup.com/docs/quote-option -- 120 requests/minute
OPTION_TRADE_TICKS_LIMITER = RateLimiter(120, "get_option_trade_ticks")

#: https://docs-en.itigerup.com/docs/quote-option -- 60 requests/minute
OPTION_BARS_LIMITER = RateLimiter(60, "get_option_bars")
OPTION_ANALYSIS_LIMITER = RateLimiter(60, "get_option_analysis")

#: https://docs-en.itigerup.com/docs/quote-stock -- 60 requests/minute
BARS_LIMITER = RateLimiter(60, "get_bars")

#: https://docs-en.itigerup.com/docs/quote-stock -- 120 requests/minute
TRADE_TICKS_LIMITER = RateLimiter(120, "get_trade_ticks")

#: https://docs-en.itigerup.com/docs/quote-stock -- 10 requests/minute
MARKET_STATUS_LIMITER = RateLimiter(10, "get_market_status")

#: https://docs-en.itigerup.com/docs/quote-common -- 10 requests/minute.
#: get_quote_permission only reports entitlements. Its sibling
#: grab_quote_permission CHANGES which device holds market data access, so it
#: is deliberately not given a limiter here: nothing in this project calls it.
QUOTE_PERMISSION_LIMITER = RateLimiter(10, "get_quote_permission")
KLINE_QUOTA_LIMITER = RateLimiter(10, "get_kline_quota")

#: https://docs-en.itigerup.com/docs/accounts -- 60 requests/minute
MANAGED_ACCOUNTS_LIMITER = RateLimiter(60, "get_managed_accounts")
PRIME_ASSETS_LIMITER = RateLimiter(60, "get_prime_assets")
POSITIONS_LIMITER = RateLimiter(60, "get_positions")

#: https://docs-en.itigerup.com/docs/orderinfo -- 120 requests/minute
ORDERS_LIMITER = RateLimiter(120, "get_orders")
OPEN_ORDERS_LIMITER = RateLimiter(120, "get_open_orders")

#: https://docs-en.itigerup.com/docs/get-contract -- 60 requests/minute.
#: Contract lookup is a different entitlement from quote data, and this
#: account has it, so these are the calls Phase 3 leans on.
CONTRACT_LIMITER = RateLimiter(60, "get_contract")
DERIVATIVE_CONTRACTS_LIMITER = RateLimiter(60, "get_derivative_contracts")

#: https://docs-en.itigerup.com/docs/place-order -- 120 requests/minute.
PLACE_ORDER_LIMITER = RateLimiter(120, "place_order")

#: https://docs-en.itigerup.com/docs/modify-order -- 120 requests/minute.
CANCEL_ORDER_LIMITER = RateLimiter(120, "cancel_order")


# ---------------------------------------------------------------------------
# The clients
# ---------------------------------------------------------------------------

_SDK_MISSING = (
    "The tigeropen SDK is not installed in this environment.\n"
    "Install it with:  pip install -r requirements.txt"
)


class ClientSetupError(Exception):
    """The SDK is unavailable or the credentials could not be loaded."""


def build_client_config(settings: Settings) -> "TigerOpenClientConfig":
    """Build a TigerOpenClientConfig from validated settings.

    The SDK is imported lazily so that configuration and safety logic remain
    importable -- and unit-testable -- without the SDK present.
    """
    try:
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ClientSetupError(_SDK_MISSING) from exc

    client_config = TigerOpenClientConfig()

    try:
        client_config.private_key = read_private_key(str(settings.private_key_path))
    except Exception as exc:
        # Never surface the key's contents, only its location.
        raise ClientSetupError(
            f"Could not read the private key at {settings.private_key_path}.\n"
            "Check that it is the PKCS#8 PEM file downloaded from the Tiger "
            f"developer page and that it is not truncated. ({type(exc).__name__})"
        ) from exc

    client_config.tiger_id = settings.tiger_id
    client_config.account = settings.account
    if settings.license:
        client_config.license = settings.license

    return client_config


def build_quote_client(settings: Settings, grab_permission: bool = False) -> "QuoteClient":
    """Market data client. Read-only, and by default free of side effects.

    Note the default differs from the SDK's. QuoteClient claims market data
    device access on construction unless told otherwise, which is not a
    purchase and grants no entitlement, but *moves* primary-device status to
    this machine and takes it from whatever held it before -- typically the
    Tiger app on a phone. Only one device holds it at a time. Nothing in this
    project needs it, so it is off here.

    Args:
        settings: Validated configuration.
        grab_permission: Whether to claim market data device access. Pass True
            deliberately when a real-time call has been refused with "current
            device does not have permission", which is a different failure from
            an unbought entitlement.

    Returns:
        A configured QuoteClient.
    """
    try:
        from tigeropen.quote.quote_client import QuoteClient
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ClientSetupError(_SDK_MISSING) from exc

    return QuoteClient(
        build_client_config(settings),
        is_grab_permission=grab_permission,
    )


def build_trade_client(settings: Settings) -> "TradeClient":
    """Account and trading client.

    In this build it is used only for read-only account queries.
    """
    try:
        from tigeropen.trade.trade_client import TradeClient
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ClientSetupError(_SDK_MISSING) from exc

    return TradeClient(build_client_config(settings))
