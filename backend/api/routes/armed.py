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

from fastapi import APIRouter

from backend.core import armed

from ..schemas import ArmedSettingIn, ArmedSettingOut
from ..shared import get_settings

router = APIRouter(tags=["settings"])


def describe() -> ArmedSettingOut:
    """Build the switch's row, saying which way it is and who set it.

    Returns:
        The row.
    """
    settings = get_settings()
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
def read_armed() -> ArmedSettingOut:
    """Return whether orders may currently reach the broker."""
    return describe()


@router.put("/trade/armed", response_model=ArmedSettingOut)
def write_armed(body: ArmedSettingIn) -> ArmedSettingOut:
    """Flip the switch, overriding .env until it is cleared.

    Args:
        body: armed=True to send orders, False to price them only.

    Returns:
        The row as it now stands.
    """
    armed.save(dry_run=not body.armed)
    return describe()


@router.delete("/trade/armed", response_model=ArmedSettingOut)
def clear_armed() -> ArmedSettingOut:
    """Forget the switch, so DRY_RUN in .env decides again."""
    armed.clear()
    return describe()
