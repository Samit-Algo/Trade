"""Positions and their profit and loss. Read-only."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query

from api.service.position import (
    DEFAULT_EXPIRY_WARNING_DAYS,
    list_option_positions,
    value_position,
)
from api.service.market import BidSnapshot, QuoteSource

from ..wiring import get_settings, get_trade_client
from ..errors import ApiError
from ..schemas import PositionsResponse
from ..schemas import shape_position

router = APIRouter(tags=["positions"])


def parse_bid_arguments(bid_arguments: list[str]) -> dict[str, float]:
    """Parse repeated ?bid=IDENTIFIER:PRICE parameters.

    Args:
        bid_arguments: Raw values, each "IDENTIFIER:PRICE".

    Returns:
        A mapping of identifier to bid price.

    Raises:
        ApiError: 400 when a value is not in the expected shape.
    """
    bids = {}

    for raw in bid_arguments:
        identifier, separator, price_text = raw.rpartition(":")
        if not separator:
            raise ApiError(
                status_code=400,
                error_code="BID_MALFORMED",
                message=f"Could not read {raw!r} as a bid. Use the form "
                        "bid=IDENTIFIER:PRICE, e.g. "
                        "bid=AAPL  260918C00360000:0.27",
            )
        try:
            bids[identifier.strip()] = float(price_text)
        except ValueError:
            raise ApiError(
                status_code=400,
                error_code="BID_MALFORMED",
                message=f"{price_text!r} in {raw!r} is not a number.",
            ) from None

    return bids


@router.get("/positions", response_model=PositionsResponse)
def read_positions(
    bid: list[str] = Query(
        default=[],
        description="Optional, repeatable: IDENTIFIER:PRICE. A position can only "
        "be valued if you supply the current bid, because a position is worth "
        "what someone will PAY for it and this account cannot fetch quotes. "
        "Positions without a supplied bid come back with valuation null -- never "
        "a zero standing in for an unknown.",
    ),
    expiry_warning_days: int = Query(
        default=DEFAULT_EXPIRY_WARNING_DAYS,
        description="Flag positions with this many days left or fewer.",
    ),
) -> PositionsResponse:
    """List option positions, valuing any for which a bid was supplied.

    Args:
        bid: Repeated IDENTIFIER:PRICE values.
        expiry_warning_days: Threshold for the expiring_soon flag.

    Returns:
        The positions payload.
    """
    settings = get_settings()
    supplied_bids = parse_bid_arguments(bid)
    positions = list_option_positions(get_trade_client())

    rows = []
    total_cost_basis = 0.0
    total_current_value = 0.0
    total_unrealised = 0.0
    every_position_valued = True

    for position in positions:
        valuation = None
        bid_price = supplied_bids.get(position.identifier.strip())

        if bid_price is not None:
            # Stamped MANUAL because a human read this number off a screen.
            # Arriving over HTTP does not make it fetched.
            snapshot = BidSnapshot(
                bid=bid_price,
                source=QuoteSource.MANUAL,
                captured_at=datetime.now(timezone.utc),
            )
            valuation = value_position(
                position, snapshot, settings.preview_token_ttl_seconds
            )
            total_current_value += valuation.current_value
            total_unrealised += valuation.unrealised_pnl
        else:
            every_position_valued = False

        row = shape_position(position, expiry_warning_days, valuation)
        total_cost_basis += row.cost_basis
        rows.append(row)

    return PositionsResponse(
        positions=rows,
        expiry_warning_days=expiry_warning_days,
        total_cost_basis=round(total_cost_basis, 2),
        total_current_value=(
            round(total_current_value, 2) if every_position_valued and rows else None
        ),
        total_unrealised_pnl=(
            round(total_unrealised, 2) if every_position_valued and rows else None
        ),
    )
