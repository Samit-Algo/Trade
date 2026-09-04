"""Contract resolution. Read-only, and identity only."""

from __future__ import annotations

from fastapi import APIRouter, Query

from tiger_backend.contracts import find_option_contract

from ..deps import get_quote_client, get_trade_client
from ..models import ContractOut
from ..shaping import shape_contract

router = APIRouter(tags=["contracts"])


@router.get("/contracts/resolve", response_model=ContractOut)
def resolve_contract(
    underlying: str = Query(description="Underlying symbol, e.g. AAPL"),
    expiry: str = Query(description="Expiry as YYYY-MM-DD. Must be a date Tiger lists."),
    strike: float = Query(description="Strike price"),
    option_type: str = Query(description="CALL or PUT"),
) -> ContractOut:
    """Turn a human request into exactly one verified contract.

    No market data is involved: this returns identity and metadata only. The
    strike is verified by Tiger itself, which refuses one that does not exist,
    so the check is the exchange's answer rather than a local guess.

    Distinct failures get distinct statuses so a client need not read prose:
    404 for an expiry Tiger does not list, 410 for one that is listed but has
    already passed, 422 for a strike that does not exist.

    Args:
        underlying: Underlying symbol.
        expiry: Expiry as YYYY-MM-DD.
        strike: Strike price.
        option_type: CALL or PUT.

    Returns:
        The verified contract.
    """
    contract = find_option_contract(
        get_quote_client(),
        get_trade_client(),
        underlying,
        option_type,
        strike,
        expiry,
    )
    return shape_contract(contract)
