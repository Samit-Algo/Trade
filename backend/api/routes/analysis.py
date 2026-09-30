"""The Analysis page's numbers: GET /analysis. Read-only.

Reads the order history and the prices recorded while each trade was held
(logs/prices) -- no bars are fetched, so a week's report does not spend the
broker's rate limit.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Query

from backend.services import analysis
from backend.services.market.price_log import PRICE_LOG
from backend.services.order import journey as journey_service

from ..errors import ApiError
from ..shared import get_market
from .orders import read_order_history

router = APIRouter(tags=["analysis"])

#: The widest range one report covers. The history holds the most recent
#: orders only, so a longer range would quietly be incomplete.
MAX_RANGE_DAYS = 62

#: Orders read from the history for a report.
HISTORY_LIMIT = 300


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(status_code=422, error_code="DATE_INVALID",
                       message=f"{name} must be YYYY-MM-DD (got {value!r}).") from None


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _path(row: dict) -> list[tuple[int, float]]:
    """The recorded prices from the fill to the exit; [] when none were recorded."""
    filled, entry = _moment(row.get("filled_at")), row.get("fill_price")
    if not filled or not entry:
        return []
    exited = _moment(row.get("exited_at"))
    end = exited or datetime.now(timezone.utc)
    entry_ms, end_ms = int(filled.timestamp() * 1000), int(end.timestamp() * 1000)

    ticks = PRICE_LOG.read(row["identifier"], entry_ms, end_ms)
    if not ticks:
        return []
    _lead, path, _source = journey_service.build_path(
        [], ticks, entry_ms=entry_ms, entry_price=entry,
        end_ms=end_ms if exited else None,
        end_price=row.get("exit_price") if exited else None,
    )
    return [(point.time_ms, point.price) for point in path]


@router.get("/analysis")
def read_analysis(
    market: str | None = Query(default=None, description="US or IN. Omitted means US."),
    start: str | None = Query(default=None, description="First market day, YYYY-MM-DD. Omitted: today."),
    end: str | None = Query(default=None, description="Last market day, YYYY-MM-DD. Omitted: same as start."),
) -> dict:
    """Results, a heat map, and why stop losses fired, for a range of market days.

    Gross figures: no commission is deducted, for either market.
    """
    chosen = get_market(market)
    clock = chosen.profile.timezone
    first = _day(start, "start") or chosen.profile.today()
    last = _day(end, "end") or first
    if last < first:
        raise ApiError(status_code=422, error_code="DATE_RANGE_BACKWARDS",
                       message=f"The range runs backwards: {first} to {last}.")
    if (last - first).days > MAX_RANGE_DAYS:
        raise ApiError(status_code=422, error_code="DATE_RANGE_TOO_LONG",
                       message=f"At most {MAX_RANGE_DAYS} days per report.")

    history = read_order_history(limit=HISTORY_LIMIT, fresh=False, market=chosen.profile.id)
    rows = [
        row for row in history.model_dump(mode="json")["orders"]
        if (placed := _moment(row.get("placed_at")))
        and first <= placed.astimezone(clock).date() <= last
    ]

    fallback = chosen.profile.contract_multiplier
    if chosen.profile.id != "US":
        fallback = float(getattr(chosen.settings, "lot_size", fallback) or fallback)

    report = analysis.build(
        rows,
        {row["order_id_text"]: _path(row) for row in rows
         if row.get("fill_price") and row.get("outcome") == "STOPPED_OUT"},
        timezone=clock, opens=chosen.profile.session.opens, fallback_multiplier=fallback,
    )
    return {
        "market": chosen.profile.id,
        "currency_symbol": chosen.profile.currency_symbol,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "today": chosen.profile.today().isoformat(),
        **report,
    }
