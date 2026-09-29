"""Liveness, and which account this service is pointed at."""

from __future__ import annotations

from fastapi import APIRouter

from backend.core import armed
from ..shared import (
    DEFAULT_MARKET,
    available_markets,
    get_market,
    get_settings,
    orders_are_enabled,
)
from ..schemas import HealthResponse, MarketOut, MarketsResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def read_health() -> HealthResponse:
    """Report service state and whether orders are currently possible.

    Returns:
        The health payload. The account appears masked to its last four
        digits; the full number is never returned over HTTP.
    """
    settings = get_settings()
    enabled, _reason = orders_are_enabled(settings)

    return HealthResponse(
        status="ok",
        mode=settings.mode,
        dry_run=armed.is_dry(settings),
        orders_enabled=enabled,
        market_data_source=settings.market_data_source,
        account_masked=settings.masked_account,
    )


@router.get("/markets", response_model=MarketsResponse)
def read_markets() -> MarketsResponse:
    """List the markets this service trades, for the page's US / India switch.

    Returns:
        Each market with its currency, clock, symbols and its own SAFE/ARMED
        state. A market listed as not ready has settings but cannot trade.
    """
    rows = []
    for market_id in available_markets():
        market = get_market(market_id)
        settings = market.settings
        rows.append(
            MarketOut(
                id=market.profile.id,
                name=market.profile.name,
                currency=market.profile.currency,
                currency_symbol=market.profile.currency_symbol,
                timezone=str(market.profile.timezone),
                ready=market.ready,
                dry_run=armed.is_dry(settings),
                mode=settings.mode,
                trade_symbols=list(settings.trade_symbols),
                quick_sell_steps=list(settings.quick_sell_steps),
                option_tick_size=settings.option_tick_size,
                alerts=market.alerts(),
            )
        )
    return MarketsResponse(default=DEFAULT_MARKET, markets=rows)
