"""Market data: what contracts exist for an underlying. Read-only."""

from __future__ import annotations

from fastapi import APIRouter

from api.service.market import fetch_spot_price, list_expirations

from ..schemas import ExpirationsResponse, ExpiryOut, SpotPriceResponse
from ..errors import ApiError
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


@router.get("/spot/{underlying}", response_model=SpotPriceResponse)
def read_spot_price(underlying: str) -> SpotPriceResponse:
    """Fetch the underlying's live share price, for choosing a strike.

    NOT from Tiger. This account holds no usStockQuote entitlement, so Tiger's
    price is roughly 15 minutes stale -- enough to pick a different strike, as
    a measured $2.10 gap on TSLA showed. Yahoo answers in 1-3 seconds.

    This is a convenience, never a dependency: it is used to fill a form field
    that stays editable, and a failure here means the price is typed by hand
    exactly as it was before.

    Args:
        underlying: Underlying symbol, e.g. AAPL.

    Returns:
        The price, its age, and whether it is currently trading.

    Raises:
        ApiError: 502 when no host would answer.
    """
    spot = fetch_spot_price(underlying)

    if spot is None:
        raise ApiError(
            status_code=502,
            error_code="SPOT_PRICE_UNAVAILABLE",
            message=(
                f"No live price could be fetched for {underlying.upper()}. "
                "Type it from the Tiger app instead."
            ),
        )

    return SpotPriceResponse(
        symbol=spot.symbol,
        price=spot.price,
        age_seconds=spot.age_seconds,
        is_live=spot.is_live,
        source=spot.source,
        note=(
            f"Live from Yahoo, {spot.age_seconds:.0f}s old."
            if spot.is_live
            else "Market is closed; this is the last trade being held, not a "
                 "currently-trading price."
        ),
    )
