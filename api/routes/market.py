"""Market data: what contracts exist for an underlying. Read-only."""

from __future__ import annotations

from fastapi import APIRouter

from api.service.market import list_expirations

from ..schemas import ExpirationsResponse, ExpiryOut
from ..shared import get_quote_client

router = APIRouter(tags=["market"])


@router.get("/expirations/{underlying}", response_model=ExpirationsResponse)
def read_expirations(underlying: str) -> ExpirationsResponse:
    """List every expiration date Tiger reports for an underlying.

    Nothing here constructs a date. Tiger returns recently expired dates in
    this list, so each row carries an `expired` flag rather than being
    silently filtered out -- a caller should see what the broker actually said.

    Args:
        underlying: Underlying symbol, e.g. AAPL.

    Returns:
        The expirations payload.
    """
    quote_client = get_quote_client()
    expiries = list_expirations(quote_client, underlying.upper())

    rows = []
    for expiry in expiries:
        rows.append(
            ExpiryOut(
                date=expiry.date_text,
                days_to_expiry=expiry.days_to_expiry,
                period=expiry.period_label,
                expired=expiry.days_to_expiry < 0,
            )
        )

    return ExpirationsResponse(underlying=underlying.upper(), expirations=rows)
