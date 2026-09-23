"""The clock-following bracket schedule: GET and PUT /trade/time-brackets.

WHY IT IS A ROUTE AND NOT JUST .env. .env is read once at startup, so
changing a window there needs a restart -- during a session that is the one
thing you cannot afford. This is the same reasoning that put the per-symbol
settings on the server rather than in the browser.

.env STILL HOLDS THE DEFAULTS. Saving here overrides them; DELETE clears the
override and .env comes back, including any edit made to it since.

WHAT IT CANNOT DO. Change a symbol's own bracket. Those stay on
/trade/symbols and keep outranking this -- a symbol someone tuned keeps its
bracket all session.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from api.service.core import time_bracket_settings
from api.service.core.time_brackets import (
    TimeBracketError,
    resolve,
    session_start,
)

from ..errors import ApiError
from ..schemas import (
    TimeBracketSettingIn,
    TimeBracketSettingOut,
    TimeBracketWindowOut,
)
from ..shared import get_settings

router = APIRouter(tags=["settings"])


def describe(settings) -> TimeBracketSettingOut:
    """Build the schedule row, including what it means at this moment.

    The active window is resolved the same way prepare_trade resolves it, so
    the page shows the bracket a trade placed now would really use.

    Args:
        settings: The loaded configuration.

    Returns:
        The row.
    """
    schedule = time_bracket_settings.effective(settings)

    now = datetime.now(schedule.timezone)
    opened = session_start(now, schedule.start).astimezone(schedule.timezone)

    active = (
        resolve(
            now,
            windows=schedule.windows,
            start=schedule.start,
            display_timezone=schedule.timezone,
        )
        if schedule.enabled
        else None
    )

    if not schedule.enabled:
        status = (
            "Off. Brackets come from the per-symbol values and the "
            "configured default, as before."
        )
    elif active is None:
        status = (
            f"Waiting. Today's schedule starts at {opened:%H:%M} "
            f"{schedule.timezone_name}; until then a trade uses the "
            "configured default."
        )
    else:
        remaining = active.minutes_remaining
        tail = (
            "runs to the close"
            if remaining is None
            else f"switches in {remaining} min"
        )
        status = (
            f"Window {active.index + 1} of {active.total} -- "
            f"TP {active.take_profit:g}% / SL {active.stop_loss:g}% -- {tail}"
        )

    return TimeBracketSettingOut(
        enabled=schedule.enabled,
        timezone=schedule.timezone_name,
        start=schedule.start_text or "MARKET_OPEN",
        windows=schedule.windows_text,
        parsed_windows=[
            TimeBracketWindowOut(
                minutes=window.minutes,
                take_profit=window.take_profit,
                stop_loss=window.stop_loss,
            )
            for window in schedule.windows
        ],
        source=schedule.source,
        env_enabled=settings.time_brackets_enabled,
        env_timezone=settings.time_brackets_timezone,
        env_start=settings.time_brackets_start,
        env_windows=settings.time_brackets_windows,
        now=f"{now:%H:%M}",
        session_start=f"{opened:%H:%M}",
        active_index=None if active is None else active.index,
        active_take_profit=None if active is None else active.take_profit,
        active_stop_loss=None if active is None else active.stop_loss,
        minutes_remaining=None if active is None else active.minutes_remaining,
        status=status,
    )


@router.get("/trade/time-brackets", response_model=TimeBracketSettingOut)
def read_time_brackets() -> TimeBracketSettingOut:
    """Return the schedule in force and what it means right now.

    Returns:
        The saved schedule if there is one, otherwise the .env one, with the
        active window resolved for this moment.
    """
    return describe(get_settings())


@router.put("/trade/time-brackets", response_model=TimeBracketSettingOut)
def write_time_brackets(body: TimeBracketSettingIn) -> TimeBracketSettingOut:
    """Store the schedule, overriding .env until it is cleared.

    Args:
        body: The whole schedule. Saved together, because the four fields
            only mean anything as a set.

    Returns:
        The row as it now stands.

    Raises:
        ApiError: 422 when the timezone, start or windows cannot be used.
            Nothing is written in that case.
    """
    try:
        time_bracket_settings.save(
            enabled=body.enabled,
            timezone_name=body.timezone,
            start_text=body.start,
            windows_text=body.windows,
        )
    except TimeBracketError as error:
        raise ApiError(
            status_code=422,
            error_code="TIME_BRACKET_INVALID",
            message=str(error),
        ) from error

    return describe(get_settings())


@router.delete("/trade/time-brackets", response_model=TimeBracketSettingOut)
def clear_time_brackets() -> TimeBracketSettingOut:
    """Forget the saved schedule, so the .env one comes back.

    Deleting rather than saving a copy of .env is deliberate: it restores
    .env as it is READ, including any later edit to it.

    Returns:
        The row as it now stands, which is the .env schedule.
    """
    time_bracket_settings.clear()
    return describe(get_settings())
