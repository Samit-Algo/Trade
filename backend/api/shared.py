"""Shared plumbing: the objects and helpers every route needs.

Routes reach a broker only through a Market (`get_market`). Each market is
built once and cached, and builds its own broker clients lazily -- they are
expensive, each one reads and parses the private key.

Nothing here contains business logic. It hands routes the same objects the
CLI scripts build for themselves, so both entry points call the same library
functions with the same inputs.

Request logging lives here rather than in `main.py` for an import reason:
routes need it, and `main.py` imports the routes, so putting it there would
make the graph circular.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend.core import armed, paths
from backend.core.config import Settings, load_settings
from backend.markets.base import Market, UnknownMarket

from .order_rules import IdempotencyStore

# REENTRANT on purpose. get_market() holds this lock and then calls
# get_settings(), which takes it again on the same thread. A plain Lock
# deadlocks there -- and it deadlocks on the first real request, not at
# import, so it looks like a hang rather than a crash.
_lock = threading.RLock()
_settings: Settings | None = None
_markets: dict[str, Market] = {}
_idempotency_store: IdempotencyStore | None = None
_contract_cache: dict = {}


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


#: A request that names no market means this one, so every caller written
#: before there was a second market -- the TradingView userscript included --
#: keeps meaning what it meant.
DEFAULT_MARKET = "US"


def available_markets() -> list[str]:
    """The markets this service trades, in the order the page lists them.

    US always. India when config/india.env exists -- the file is what turns
    it on, so deleting it turns India off without touching code.
    """
    from backend.markets.india.config import india_env_path

    return ["US"] + (["IN"] if india_env_path().exists() else [])


def get_market(market_id: str | None = DEFAULT_MARKET) -> Market:
    """Return the market a request is about, built once.

    THE REGISTRY: the one place a market is chosen by name. Everything else
    asks this and never imports a market's folder.

    Args:
        market_id: Which market, e.g. "US" or "IN". None or blank means US.

    Returns:
        The Market. Its broker clients are built on first use, not here.

    Raises:
        UnknownMarket: For a market this service does not trade.
        ConfigError: When the market's settings file cannot be used.
    """
    wanted = (market_id or DEFAULT_MARKET).strip().upper()
    with _lock:
        if wanted not in _markets:
            if wanted not in available_markets():
                raise UnknownMarket(
                    f"No market named {market_id!r}. This service trades: "
                    f"{', '.join(available_markets())}."
                )
            _markets[wanted] = _build_market(wanted)
        return _markets[wanted]


def _build_market(market_id: str) -> Market:
    """Construct one market from its settings. Called under the lock."""
    if market_id == "US":
        from backend.markets.us.market import UsMarket

        return UsMarket(get_settings())

    from backend.markets.india.config import load_india_settings
    from backend.markets.india.market import IndiaMarket

    return IndiaMarket(load_india_settings())


def get_idempotency_store() -> IdempotencyStore:
    """Return the shared idempotency store, built once.

    Returns:
        The store, with its TTL taken from settings.
    """
    global _idempotency_store
    with _lock:
        if _idempotency_store is None:
            _idempotency_store = IdempotencyStore(
                ttl_seconds=get_settings().idempotency_ttl_seconds
            )
        return _idempotency_store


def get_cached_contract(key: tuple, build):
    """Return a resolved contract, resolving it at most once per market day.

    An OptionContractInfo is stable for the day: identifier, contract_id,
    strike and multiplier do not move. Only days_to_expiry does, so the market
    date is part of the key and yesterday's entries simply stop being found.

    This is what takes the fast path from six network calls to one. Resolution
    is three round trips, and repeating them for every trade on the same
    contract buys nothing.

    Args:
        key: Whatever identifies the contract, e.g. (symbol, expiry, strike, side).
        build: Called with no arguments to resolve it on a miss.

    Returns:
        The cached or freshly built contract.
    """
    market = get_market()
    dated_key = (market.profile.id, market.profile.today().isoformat()) + tuple(key)
    with _lock:
        if dated_key in _contract_cache:
            return _contract_cache[dated_key]

    # Built OUTSIDE the lock: resolution makes network calls, and holding the
    # shared lock across them would serialise every request in the process.
    built = build()

    with _lock:
        _contract_cache[dated_key] = built
        return built


def orders_are_enabled(settings: Settings) -> tuple[bool, str]:
    """Report whether an order would actually be sent, and why not.

    Read by GET /health and shown in the UI so the state is visible before
    anyone presses anything. It decides nothing: DRY_RUN is what POST /trade
    branches on, and assert_order_allowed is the guard before the wire.

    Args:
        settings: The validated settings.

    Returns:
        A pair of (enabled, reason when not enabled).
    """
    if armed.is_dry(settings):
        _, source = armed.describe(settings)
        return False, (
            f"The switch is SAFE ({source}), so no order can be submitted. "
            "This is Lock 3, and it is on by default. Arm it from the page, "
            "or set DRY_RUN=false in .env, only when you intend to place "
            "real orders."
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
# in backend/core/audit.py already records WHAT the order was; this records
# WHERE the request came from, alongside it.
#
# It lives here rather than in main.py for an import reason: routes need it, and
# main.py imports the routes, so putting it there would make the graph circular.
# ---------------------------------------------------------------------------

LOG_DIRECTORY = paths.LOG_DIRECTORY
API_LOG_PATH = paths.API_LOG_PATH

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

