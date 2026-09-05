"""Whose account is this, and what is in it?

Read-only. One endpoint.

*What can this account actually reach?* used to be answered here too, by a
`/capabilities` pair that cached a twenty-call probe. It was removed: nothing
consumed it over HTTP, and `scripts/00_check_capabilities.py` answers the same
question better, in the place you actually ask it -- at the terminal, once,
after buying a market-data package.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.service.core.broker import MANAGED_ACCOUNTS_LIMITER, PRIME_ASSETS_LIMITER
from api.service.position import SECURITIES_SEGMENT

from ..schemas import AccountResponse
from ..shared import get_settings, get_trade_client

router = APIRouter(tags=["account"])


@router.get("/account", response_model=AccountResponse)
def read_account() -> AccountResponse:
    """Report what Tiger says about the configured account.

    Cash available is reported and buying power is reported beside it, but
    nothing in this project costs an order against buying power: on a Reg T
    margin account that figure is roughly four times the cash and the
    difference is borrowed.

    Returns:
        The account payload.
    """
    settings = get_settings()
    trade_client = get_trade_client()

    MANAGED_ACCOUNTS_LIMITER.wait()
    profiles = trade_client.get_managed_accounts()

    matching_profile = None
    for profile in profiles or []:
        if getattr(profile, "account", None) == settings.account:
            matching_profile = profile
            break

    PRIME_ASSETS_LIMITER.wait()
    portfolio = trade_client.get_prime_assets(base_currency="USD")
    segments = getattr(portfolio, "segments", None) or {}
    segment = segments.get(SECURITIES_SEGMENT)

    def read(source, name):
        """Read an optional attribute off an SDK object."""
        if source is None:
            return None
        return getattr(source, name, None)

    return AccountResponse(
        account_masked=settings.masked_account,
        account_type=read(matching_profile, "account_type"),
        status=read(matching_profile, "status"),
        capability=read(matching_profile, "capability"),
        currency=read(segment, "currency"),
        cash_available_for_trade=read(segment, "cash_available_for_trade"),
        buying_power=read(segment, "buying_power"),
        net_liquidation=read(segment, "net_liquidation"),
    )
