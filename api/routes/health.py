"""Liveness, and which account this service is pointed at."""

from __future__ import annotations

from fastapi import APIRouter

from ..wiring import get_settings, orders_are_enabled
from ..schemas import HealthResponse

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
        dry_run=settings.dry_run,
        orders_enabled=enabled,
        market_data_source=settings.market_data_source,
        account_masked=settings.masked_account,
    )
