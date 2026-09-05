"""Shared plumbing: the objects and helpers every route needs.

The Tiger clients are expensive to construct -- each one reads and parses the
private key -- and the SDK's own docs recommend one module-level QuoteClient
reused rather than many. So they are built lazily on first use and cached.

Nothing here contains business logic. It hands routes the same objects the
CLI scripts build for themselves, so both entry points call the same library
functions with the same inputs.

Request logging lives here rather than in `app.py` for an import reason:
routes need it, and `app.py` imports the routes, so putting it there would
make the graph circular.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from api.service.core.broker import build_quote_client, build_trade_client
from api.service.core.config import Settings, load_settings

from .order_rules import PreviewTokenStore

# REENTRANT on purpose. get_quote_client() holds this lock and then calls
# get_settings(), which takes it again on the same thread. A plain Lock
# deadlocks there -- and it deadlocks on the first real request, not at
# import, so it looks like a hang rather than a crash.
_lock = threading.RLock()
_settings: Settings | None = None
_quote_client = None
_trade_client = None
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


# ---------------------------------------------------------------------------
# Request logging
#
# An HTTP port that can place orders needs to record who asked. The audit trail
# in service/core/audit.py already records WHAT the order was; this records
# WHERE the request came from, alongside it.
#
# It lives here rather than in app.py for an import reason: routes need it, and
# app.py imports the routes, so putting it there would make the graph circular.
# ---------------------------------------------------------------------------

LOG_DIRECTORY = Path(__file__).resolve().parent.parent / "logs"
API_LOG_PATH = LOG_DIRECTORY / "api_requests.log"

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return the API request logger, configuring it once.

    Returns:
        A logger writing to logs/api_requests.log.
    """
    global _logger
    if _logger is not None:
        return _logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("tiger_api")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.FileHandler(API_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def log_order_request(request, action: str, subject: str, cash: float | None) -> None:
    """Record an order-path request with the address that made it.

    Args:
        request: The FastAPI request, for the client address.
        action: preview, submit or cancel.
        subject: The contract identifier or order ID.
        cash: The cash figure involved, when there is one.
    """
    client_host = request.client.host if request.client else "unknown"
    cash_text = f"{cash:.2f}" if cash is not None else "-"

    get_logger().info(
        "action=%s client=%s subject=%s cash=%s at=%s",
        action,
        client_host,
        subject,
        cash_text,
        datetime.now(timezone.utc).isoformat(),
    )

