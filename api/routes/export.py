"""Downloading order history as a spreadsheet: GET /orders/export.

Kept apart from the history route on purpose. That route feeds a page that
polls every second; this one builds a file a human asked for. They share the
same data and nothing else, so columns and sheets can be added here without
touching anything on the trading path.

THE DAY IS THE MARKET'S DAY. A trading session is a market-day concept, and
filtering on the viewer's own timezone would put every US afternoon trade on
the following day for anyone east of New York -- which is the bug this
project already fixed once, in the history page's date filter.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Query, Response

from api.service.export import build_workbook, realised_by_contract
from api.service.export.columns import MARKET_TIMEZONE
from api.service.core.broker import ORDERS_LIMITER
from tigeropen.common.consts import SecurityType

from ..errors import ApiError
from ..shared import get_settings, get_trade_client
from .orders import read_order_history

router = APIRouter(tags=["orders"])

#: Excel's own media type. Anything else and a browser offers to save it as
#: an unknown file, or a spreadsheet refuses to open it.
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

#: Enough history to cover the requested range. The endpoint filters by day
#: after fetching, so this is the ceiling on how far back a range can reach.
EXPORT_ORDER_LIMIT = 300


def parse_day(value: str | None, name: str) -> date | None:
    """Read a YYYY-MM-DD query parameter.

    Args:
        value: The supplied string, or None.
        name: The parameter name, for the message.

    Returns:
        The date, or None when nothing was supplied.

    Raises:
        ApiError: 422 when it is not a date. Guessing at a malformed date
            would export a period nobody asked for.
    """
    if not value:
        return None

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ApiError(
            status_code=422,
            error_code="DATE_INVALID",
            message=f"{name} must be written as YYYY-MM-DD (got {value!r}).",
        )

    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            error_code="DATE_INVALID",
            message=f"{name} is not a real date ({value!r}).",
        ) from error


def market_day_of(stamp) -> date | None:
    """The market day an order belongs to.

    Args:
        stamp: A datetime, or an ISO-8601 string, or None. model_dump
            leaves datetimes as datetimes, so both arrive here.

    Returns:
        The date in New York, or None.
    """
    if not stamp:
        return None

    if isinstance(stamp, datetime):
        moment = stamp
    else:
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None

    return moment.astimezone(MARKET_TIMEZONE).date()


@router.get("/orders/export")
def export_orders(
    start: str | None = Query(
        default=None,
        description="First day to include, YYYY-MM-DD, in MARKET time. "
        "Omit both dates for today.",
    ),
    end: str | None = Query(
        default=None,
        description="Last day to include, YYYY-MM-DD. Omit for a single day.",
    ),
) -> Response:
    """Download the order history for a period as an .xlsx file.

    Args:
        start: First market day, or None for today.
        end: Last market day, or None for a single day.

    Returns:
        The spreadsheet, as a download.

    Raises:
        ApiError: 422 when a date is malformed or the range runs backwards.
    """
    first = parse_day(start, "start")
    last = parse_day(end, "end")

    # Today in MARKET time, which is the day these orders belong to -- not the
    # day it happens to be wherever the reader is sitting.
    today = datetime.now(MARKET_TIMEZONE).date()

    if first is None:
        first = today
    if last is None:
        last = first

    if last < first:
        raise ApiError(
            status_code=422,
            error_code="DATE_RANGE_BACKWARDS",
            message=(
                f"The range runs backwards: {first} to {last}. The end must "
                "not be before the start."
            ),
        )

    # Always fresh. An export is a deliberate act, and a file built from a
    # few-second-old cache would be a strange thing to hand someone.
    history = read_order_history(limit=EXPORT_ORDER_LIMIT, fresh=True)

    orders = [
        order
        for order in history.model_dump().get("orders", [])
        if (day := market_day_of(order.get("placed_at"))) is not None
        and first <= day <= last
    ]

    # What each contract actually made, from the FILLS. The per-order P&L in
    # the history pairs a buy with a sell, and that pairing over-counts once a
    # contract is traded more than once in a session -- measured at +1153
    # against a true +451 on one day. See service/export/realised.py.
    by_contract = {}
    try:
        settings = get_settings()
        ORDERS_LIMITER.wait()
        filled = get_trade_client().get_filled_orders(
            account=settings.account,
            sec_type=SecurityType.OPT,
            start_date=first.isoformat(),
            # The broker treats end_date as exclusive in places, so ask for a
            # day past the range and let the market-day filter do the cutting.
            end_date=(last + timedelta(days=1)).isoformat(),
        )
        by_contract = realised_by_contract(filled, first, last)
    except Exception:  # noqa: BLE001 -- the file is still worth having
        by_contract = {}

    content = build_workbook(orders, first, last, by_contract)

    name = (
        f"orders-{first}.xlsx" if first == last
        else f"orders-{first}-to-{last}.xlsx"
    )

    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            # filename* carries the UTF-8 form for anything non-ASCII; plain
            # filename is the fallback for older clients.
            "Content-Disposition": (
                f'attachment; filename="{name}"; '
                f"filename*=UTF-8''{quote(name)}"
            ),
            # So a caller can tell an empty period from a failed request.
            "X-Order-Count": str(len(orders)),
        },
    )
