"""Construction of the Tiger SDK clients.

QuoteClient (market data) and TradeClient (account and orders) are kept
strictly separate. There is deliberately no wrapper object exposing both:
market data is read-only and harmless, trading is not, and code that holds one
should not implicitly hold the other.

Every attribute set on the client config below is a documented one:
https://docs-en.itigerup.com/docs/prepare
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from tigeropen.quote.quote_client import QuoteClient
    from tigeropen.trade.trade_client import TradeClient
    from tigeropen.tiger_open_config import TigerOpenClientConfig

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


def build_quote_client(settings: Settings, grab_permission: bool = True) -> "QuoteClient":
    """Market data client. Read-only for data, but see grab_permission.

    Args:
        settings: Validated configuration.
        grab_permission: Whether to claim market data device access on
            construction. The SDK does this by default. It is not a purchase
            and grants no new entitlement -- it moves which *device* counts as
            primary, and takes that status away from whatever held it before,
            such as the Tiger app on a phone. Pass False for a strictly
            side-effect-free session, accepting that real-time data may then be
            refused with "current device does not have permission".

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
