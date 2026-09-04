"""The capability probe, callable from the API.

`scripts/00_check_capabilities.py` is the canonical version and stays exactly
as it is -- it is the verified evidence behind HANDOVER.md section 2, and it
writes the diffable report files. This module runs the same set of endpoints
and returns structured rows instead of printing a table.

Kept deliberately short and dependent on the same throttle limiters, so the
API cannot probe faster than the CLI is allowed to.
"""

from __future__ import annotations

from tiger_backend.throttle import (
    BARS_LIMITER,
    CHAIN_LIMITER,
    DELAYED_STOCK_BRIEFS_LIMITER,
    EXPIRATIONS_LIMITER,
    KLINE_QUOTA_LIMITER,
    MANAGED_ACCOUNTS_LIMITER,
    MARKET_STATUS_LIMITER,
    OPEN_ORDERS_LIMITER,
    OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER,
    ORDERS_LIMITER,
    POSITIONS_LIMITER,
    PRIME_ASSETS_LIMITER,
    QUOTE_PERMISSION_LIMITER,
    STOCK_BRIEFS_LIMITER,
    TRADE_TICKS_LIMITER,
)

from .models import CapabilityRow

#: A liquid, always-listed symbol, so a failure means a capability problem
#: rather than a badly chosen test case.
TEST_SYMBOL = "AAPL"


def classify(error: Exception) -> tuple[str, str, str | None]:
    """Decide whether a failure is a permission refusal or a real error.

    A missing entitlement is the answer to the question the probe asks, not a
    failure, so it gets its own outcome.

    Args:
        error: The exception the SDK raised.

    Returns:
        A triple of (outcome, detail, entitlement name or None).
    """
    message = str(error)

    if "permission denied" in message.lower() or "4000:" in message:
        entitlement = None
        if "US OPT" in message:
            entitlement = "usOptionQuote"
        elif "US market" in message:
            entitlement = "usStockQuote"
        return "DENIED", "permission denied", entitlement

    return "ERROR", f"{type(error).__name__}: {message}"[:200], None


def probe_one(category: str, endpoint: str, limiter, call) -> CapabilityRow:
    """Call one endpoint once and classify the result.

    Args:
        category: Group heading.
        endpoint: The SDK method name.
        limiter: The RateLimiter guarding it.
        call: A no-argument callable performing the request.

    Returns:
        One row.
    """
    limiter.wait()

    try:
        result = call()
    except Exception as error:
        outcome, detail, entitlement = classify(error)
        return CapabilityRow(
            category=category,
            endpoint=endpoint,
            outcome=outcome,
            detail=detail,
            entitlement=entitlement,
        )

    if result is None:
        detail = "returned nothing (endpoint reachable)"
    elif hasattr(result, "shape"):
        detail = f"{result.shape[0]} rows"
    elif isinstance(result, (list, tuple, dict)):
        detail = f"{len(result)} items"
    else:
        detail = "returned data"

    return CapabilityRow(
        category=category, endpoint=endpoint, outcome="OK", detail=detail
    )


def run_probe(trade_client, quote_client) -> list[CapabilityRow]:
    """Probe every endpoint this project uses.

    Strictly read-only. grab_quote_permission is never called: it moves market
    data device access to this machine and takes it from whatever held it,
    which is a modification rather than a query.

    Args:
        trade_client: A tigeropen TradeClient.
        quote_client: A tigeropen QuoteClient.

    Returns:
        One row per endpoint.
    """
    from tigeropen.common.consts import Market, SecurityType

    rows = []

    account = "Account and trading"
    rows.append(probe_one(account, "get_managed_accounts", MANAGED_ACCOUNTS_LIMITER,
                          lambda: trade_client.get_managed_accounts()))
    rows.append(probe_one(account, "get_prime_assets", PRIME_ASSETS_LIMITER,
                          lambda: trade_client.get_prime_assets(base_currency="USD")))
    rows.append(probe_one(account, "get_positions", POSITIONS_LIMITER,
                          lambda: trade_client.get_positions(sec_type=SecurityType.OPT)))
    rows.append(probe_one(account, "get_orders", ORDERS_LIMITER,
                          lambda: trade_client.get_orders(limit=1, market=Market.US)))
    rows.append(probe_one(account, "get_open_orders", OPEN_ORDERS_LIMITER,
                          lambda: trade_client.get_open_orders(market=Market.US)))

    contracts = "Contract lookup"
    rows.append(probe_one(contracts, "get_contract", CHAIN_LIMITER,
                          lambda: trade_client.get_contract(symbol=TEST_SYMBOL)))

    entitlements = "Entitlements"
    rows.append(probe_one(entitlements, "get_quote_permission", QUOTE_PERMISSION_LIMITER,
                          lambda: quote_client.get_quote_permission()))
    rows.append(probe_one(entitlements, "get_kline_quota", KLINE_QUOTA_LIMITER,
                          lambda: quote_client.get_kline_quota()))

    options = "Options market data"
    rows.append(probe_one(options, "get_option_expirations", EXPIRATIONS_LIMITER,
                          lambda: quote_client.get_option_expirations(
                              symbols=[TEST_SYMBOL], market=Market.US)))
    rows.append(probe_one(options, "get_option_chain", CHAIN_LIMITER,
                          lambda: quote_client.get_option_chain(
                              symbol=TEST_SYMBOL, expiry="2026-12-18", market=Market.US)))
    rows.append(probe_one(options, "get_option_briefs", OPTION_BRIEFS_LIMITER,
                          lambda: quote_client.get_option_briefs(
                              identifiers=["AAPL  261218C00300000"], market=Market.US)))
    rows.append(probe_one(options, "get_option_bars", OPTION_BARS_LIMITER,
                          lambda: quote_client.get_option_bars(
                              identifiers=["AAPL  261218C00300000"], market=Market.US)))

    stock = "Stock market data"
    rows.append(probe_one(stock, "get_stock_briefs", STOCK_BRIEFS_LIMITER,
                          lambda: quote_client.get_stock_briefs(symbols=[TEST_SYMBOL])))
    rows.append(probe_one(stock, "get_stock_delay_briefs", DELAYED_STOCK_BRIEFS_LIMITER,
                          lambda: quote_client.get_stock_delay_briefs(symbols=[TEST_SYMBOL])))
    rows.append(probe_one(stock, "get_bars", BARS_LIMITER,
                          lambda: quote_client.get_bars(symbols=[TEST_SYMBOL], limit=1)))
    rows.append(probe_one(stock, "get_trade_ticks", TRADE_TICKS_LIMITER,
                          lambda: quote_client.get_trade_ticks(
                              symbols=[TEST_SYMBOL], begin_index=-1, end_index=-1, limit=1)))
    rows.append(probe_one(stock, "get_market_status", MARKET_STATUS_LIMITER,
                          lambda: quote_client.get_market_status(market=Market.US)))

    return rows
