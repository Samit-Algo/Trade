"""What one contract did between entry and exit: GET /orders/{id}/journey.

WHY IT REUSES THE HISTORY ROW. The entry price, the bracket levels and the
exit are already derived, once, in read_order_history. Re-deriving them here
would be a second answer to the same question, and the day the two disagreed
the chart would draw lines the order never had. This asks that function and
adds only what it cannot know: what the price did in between.

ON DEMAND, NOT ON A TIMER. One fetch when a human opens a panel. The page
polls history every second; nothing here is on that path, and the panel
caches per trade so reopening one costs nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from api.service.market.bars import MAX_BARS, fetch_minute_bars
from api.service.order import journey as journey_service

from ..errors import ApiError
from ..schemas import BarOut, ExtremeOut, JourneyResponse
from ..shared import get_quote_client

router = APIRouter(tags=["orders"])

#: A few minutes of price before the fill, so the entry has context: a
#: trade entered into a climb reads differently from one entered into a
#: stall, and the line cannot show that if it starts at the fill.
#:
#: The run-up is only safe because the chart MARKS the entry on it and the
#: numbers below exclude it. Without both, it would look like part of the
#: trade and inflate the peak.
LEAD_MINUTES = 3

#: And a little after the exit, so the closing move is not clipped.
TRAIL_MINUTES = 2


def _parse(moment) -> datetime | None:
    """Normalise one of the history row's timestamps to aware UTC.

    The row carries real datetimes, but the same field is a string when it
    has come back through JSON -- from a test client, or a cached response.
    Both are accepted rather than assuming which, because assuming wrongly
    fails at request time on a field that is only used for drawing.

    Args:
        moment: A datetime, an ISO string, or None.

    Returns:
        An aware UTC datetime, or None when absent or unreadable.
    """
    if not moment:
        return None

    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            return None

    if not isinstance(moment, datetime):
        return None

    # A naive timestamp is UTC here: everything upstream stores UTC.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc)


def _window(row) -> tuple[datetime, datetime]:
    """Decide which minutes to ask for.

    An open position runs to now. A closed one runs to its exit. Both are
    clamped to MAX_BARS, because Tiger truncates a wider request by dropping
    its OLDEST bars -- which for a journey is the entry, the one bar that
    must not be lost.

    Args:
        row: The history row.

    Returns:
        A (begin, end) pair, aware UTC.
    """
    now = datetime.now(timezone.utc)

    begin = _parse(row.filled_at) or _parse(row.placed_at) or now
    begin -= timedelta(minutes=LEAD_MINUTES)

    exited = _parse(row.exited_at)
    end = (exited + timedelta(minutes=TRAIL_MINUTES)) if exited else now

    # Snap back to the start of the minute the fill happened in. A bar is
    # stamped at its OPEN and Tiger filters on that stamp, so a window
    # starting at 18:56:51 would exclude the 18:56:00 bar the fill is
    # inside -- on a nine-minute trade that lost every bar it had.
    #
    # This is also the whole lead-in. The chart begins at the minute the
    # contract was bought, not at some window around it.
    begin = begin.replace(second=0, microsecond=0)
    end = end.replace(second=0, microsecond=0) + timedelta(minutes=1)

    if end <= begin:
        end = begin + timedelta(minutes=1)

    # Keep the entry. A trade held longer than MAX_BARS minutes loses its
    # TAIL rather than its head, and the panel says the chart is truncated.
    if (end - begin) > timedelta(minutes=MAX_BARS):
        end = begin + timedelta(minutes=MAX_BARS)

    return begin, end


def _find_row(order_id: int):
    """Find one order in the history the rest of the page already reads.

    Args:
        order_id: The broker's order id.

    Returns:
        The history row.

    Raises:
        ApiError: 404 when no such order is listed.
    """
    from .orders import read_order_history

    history = read_order_history(limit=300)

    for row in history.orders:
        if str(row.order_id_text) == str(order_id):
            return row

    raise ApiError(
        status_code=404,
        error_code="ORDER_NOT_FOUND",
        message=(
            f"Order {order_id} is not in the order history, so there is "
            "nothing to chart."
        ),
    )


@router.get("/orders/{order_id}/journey", response_model=JourneyResponse)
def read_journey(order_id: int) -> JourneyResponse:
    """Return one trade's minute bars and what they say about it.

    Args:
        order_id: The broker's order id, as shown in the history.

    Returns:
        The bars, the levels to draw across them, and the peak and trough.

    Raises:
        ApiError: 404 when the order is not in the history.
    """
    row = _find_row(order_id)

    entry = row.fill_price or row.limit_price
    if not entry:
        # Nothing filled, so there is no journey -- only an order that sat
        # there. Answered rather than refused, so the panel can say so.
        return JourneyResponse(
            order_id=str(order_id),
            identifier=row.identifier,
            entry_price=None,
            note="This order never filled, so there is nothing to chart.",
            bars=[],
            truncated=False,
        )

    begin, end = _window(row)
    bars = fetch_minute_bars(
        get_quote_client(), row.identifier, begin=begin, end=end
    )

    # The window snaps back to the start of the minute the fill happened in,
    # so its first bar opens slightly BEFORE the position did. Searching
    # that bar for the peak can report a price the position never had -- one
    # trade showed a "best reached" before it was opened, promising profit
    # that was never reachable.
    #
    # Drawn, not measured: the bar stays on the line, out of the numbers.
    filled = _parse(row.filled_at)
    owned = (
        [b for b in bars if b.time_ms >= int(filled.timestamp() * 1000)]
        if filled else bars
    )

    result = journey_service.build(
        owned or bars,
        entry_price=entry,
        quantity=row.quantity or 1,
        multiplier=100.0,
        take_profit_price=row.take_profit_price,
        stop_loss_price=row.stop_loss_price,
    )

    exited = _parse(row.exited_at)

    return JourneyResponse(
        order_id=str(order_id),
        identifier=row.identifier,
        underlying=row.underlying,
        strike=row.strike,
        option_type=row.option_type,
        entry_price=entry,
        exit_price=row.exit_price,
        quantity=row.quantity,
        take_profit_price=row.take_profit_price,
        stop_loss_price=row.stop_loss_price,
        entry_time_ms=int((_parse(row.filled_at) or begin).timestamp() * 1000),
        exit_time_ms=int(exited.timestamp() * 1000) if exited else None,
        is_open=row.exited_at is None,
        # Every bar fetched, including the run-up before the fill: the line
        # reads better with a little context. Only the NUMBERS above are
        # restricted to bars the position actually existed for.
        bars=[
            BarOut(
                t=b.time_ms, o=b.open, h=b.high, l=b.low, c=b.close, v=b.volume
            )
            for b in bars
        ],
        best=_extreme(result.best),
        worst=_extreme(result.worst),
        progress_to_take_profit=result.progress_to_take_profit,
        drawdown_to_stop_loss=result.drawdown_to_stop_loss,
        high_water_pnl=result.high_water_pnl,
        low_water_pnl=result.low_water_pnl,
        realised_pnl=row.realised_pnl,
        unrealised_pnl=row.unrealised_pnl,
        touched_take_profit=result.touched_take_profit,
        touched_stop_loss=result.touched_stop_loss,
        truncated=(end - begin) >= timedelta(minutes=MAX_BARS),
        note=None if result.has_bars else (
            "No minute bars came back for this contract and window. It may "
            "have traded too thinly to have any."
        ),
    )


def _extreme(extreme) -> ExtremeOut | None:
    """Shape one end of the range for the wire."""
    if extreme is None:
        return None
    return ExtremeOut(
        price=extreme.price,
        time_ms=extreme.time_ms,
        percent_from_entry=round(extreme.percent_from_entry, 2),
    )
