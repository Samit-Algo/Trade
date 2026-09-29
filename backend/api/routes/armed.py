"""The arming switch: GET and PUT /trade/armed.

This is DRY_RUN, made flickable. There is no second flag -- the same value
every guard already consulted is simply readable from the page now, so
"do not trade for the first ten minutes" stops meaning "kill the server".

SAFE IS THE DEFAULT. With nothing stored, .env decides, and .env defaults to
dry run. Arming is always deliberate.

IT DOES NOT BLOCK SELLING. Nothing in the close path reads this. A switch
that stopped you exiting a position you already hold would be dangerous in
exactly the moment you most need out.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend.core import armed

from ..schemas import ArmedSettingIn, ArmedSettingOut
from ..shared import get_market

router = APIRouter(tags=["settings"])


def describe(market) -> ArmedSettingOut:
    """Build the switch's row, saying which way it is and who set it.

    Returns:
        The row.
    """
    settings = market.settings
    dry_run, source = armed.describe(settings)

    return ArmedSettingOut(
        dry_run=dry_run,
        armed=not dry_run,
        source=source,
        env_dry_run=settings.dry_run,
        mode=settings.mode,
        status=(
            "SAFE -- orders are priced and returned, nothing reaches the broker."
            if dry_run
            else f"ARMED -- orders go to the broker on the {settings.mode} account."
        ),
    )


@router.get("/trade/armed", response_model=ArmedSettingOut)
def read_armed(market: str | None = Query(default=None, description="US or IN. Omitted means US.")) -> ArmedSettingOut:
    """Return whether orders may currently reach the broker."""
    return describe(get_market(market))


@router.put("/trade/armed", response_model=ArmedSettingOut)
def write_armed(
    body: ArmedSettingIn, market: str | None = Query(default=None, description="US or IN. Omitted means US.")
) -> ArmedSettingOut:
    """Flip the switch, overriding .env until it is cleared.

    Args:
        body: armed=True to send orders, False to price them only.

    Returns:
        The row as it now stands.
    """
    chosen = get_market(market)
    armed.save(dry_run=not body.armed, market_id=chosen.profile.id)
    return describe(chosen)


@router.delete("/trade/armed", response_model=ArmedSettingOut)
def clear_armed(market: str | None = Query(default=None, description="US or IN. Omitted means US.")) -> ArmedSettingOut:
    """Forget the switch, so DRY_RUN in .env decides again."""
    chosen = get_market(market)
    armed.clear(chosen.profile.id)
    return describe(chosen)
