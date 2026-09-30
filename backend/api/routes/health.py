"""Liveness, and which account this service is pointed at."""

from __future__ import annotations

from fastapi import APIRouter

from backend.core import armed, market_switch
from ..errors import ApiError
from ..shared import (
    DEFAULT_MARKET,
    available_markets,
    get_market,
    get_settings,
    orders_are_enabled,
)
from ..schemas import HealthResponse, MarketOut, MarketSwitchIn, MarketsResponse

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
    return MarketsResponse(
        default=DEFAULT_MARKET,
        markets=[_market_row(market_id) for market_id in available_markets()],
    )


def _market_row(market_id: str) -> MarketOut:
    """One market, as the page's switch and Settings page list it."""
    market = get_market(market_id)
    settings = market.settings
    return MarketOut(
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
        switched_on=market_switch.is_on(market.profile.id),
        can_switch=market.profile.id != DEFAULT_MARKET,
    )


@router.put("/markets/{market_id}/switch", response_model=MarketOut)
def switch_market(market_id: str, body: MarketSwitchIn) -> MarketOut:
    """Switch a market on or off, from the Settings page. No restart.

    Off, nothing contacts its broker -- no price recording, no reads -- and
    a trade for it is refused. Refused while the backend is still watching
    one of its trades, because switching off would leave that trade's take
    profit unwatched -- unless `force` is sent, which the page does only
    after its warning has been confirmed.
    """
    market = get_market(market_id)
    wanted = market.profile.id
    if wanted == DEFAULT_MARKET:
        raise ApiError(
            status_code=400,
            error_code="MARKET_ALWAYS_ON",
            message=f"{market.profile.name} is the server's own market and is always on.",
        )
    if not body.on and not body.force:
        reason = market.cannot_switch_off()
        if reason:
            raise ApiError(status_code=409, error_code="MARKET_BUSY", message=reason)
    market_switch.save(body.on, wanted)
    return _market_row(wanted)
