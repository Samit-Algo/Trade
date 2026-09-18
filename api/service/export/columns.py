"""What goes in the spreadsheet, and how each value is derived.

The columns are DATA FOR ANALYSIS, not the table from the page. The page shows
what a human needs at a glance; a spreadsheet is read by sorting, filtering and
pivoting, so it carries the raw numbers behind each row as well -- the limit
that was asked for beside the price that was paid, both exits beside the one
that fired, and every timestamp rather than a friendly duration.

TIMES ARE IN MARKET TIME. A trading session is a market-day concept, and the
export is grouped and filtered by day: New York is the day these orders belong
to, whatever timezone the reader happens to sit in.

NOTHING HERE IS ESTIMATED. Commission is deliberately absent -- it is not on
the order record, and a figure computed from a rate would look like a
measurement while being an assumption. Every number in this file came from the
broker.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

#: The day an order belongs to. Same timezone the history page filters on.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

#: Shares per option contract, for turning a per-share price into cash.
CONTRACT_MULTIPLIER = 100


#: (heading, how wide to draw it). The order here is the order in the sheet.
ORDER_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Date", 12),
    ("Time", 10),
    ("Weekday", 11),
    ("Hour", 6),
    ("Symbol", 9),
    ("Strike", 9),
    ("Type", 6),
    ("Expiry", 12),
    ("Contracts", 10),
    ("Limit", 9),
    ("Filled at", 10),
    ("Slippage", 10),
    ("Exit", 9),
    ("Outcome", 17),
    ("P&L", 10),
    ("P&L %", 9),
    ("Cash in", 11),
    ("Take profit set", 15),
    ("Stop loss set", 14),
    ("TP distance %", 14),
    ("SL distance %", 14),
    ("Held (s)", 10),
    ("Held (min)", 11),
    ("Fill delay (s)", 13),
    ("Filled at time", 15),
    ("Exited at time", 15),
    ("Leg TIF", 9),
    ("Contract", 24),
    ("Order id", 20),
    ("Note", 60),
)

#: One row per bracket leg, for studying which exit actually fires.
LEG_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Date", 12),
    ("Symbol", 9),
    ("Strike", 9),
    ("Type", 6),
    ("Role", 14),
    ("Order type", 11),
    ("Price", 9),
    ("TIF", 8),
    ("Status", 12),
    ("Parent outcome", 17),
    ("Contract", 24),
    ("Leg order id", 20),
)


def _market_time(stamp) -> datetime | None:
    """Parse an ISO stamp from the API and move it to market time.

    Args:
        stamp: A datetime, or an ISO-8601 string, or None.

    Returns:
        A timezone-aware datetime in New York, or None.
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

    return moment.astimezone(MARKET_TIMEZONE)


def _percent_away(from_price, to_price):
    """How far one price sits from another, as a percentage.

    Says whether an exit was set somewhere the trade could realistically
    reach, which a raw price does not.

    Args:
        from_price: The starting price, usually the fill.
        to_price: The exit price being measured.

    Returns:
        The distance as a percentage, or None when either is missing.
    """
    if not from_price or to_price is None:
        return None

    return round((to_price - from_price) / from_price * 100, 2)


def build_order_row(order: dict) -> list:
    """Flatten one order into its spreadsheet row.

    Args:
        order: One entry from GET /orders/history.

    Returns:
        The values, in ORDER_COLUMNS order.
    """
    placed = _market_time(order.get("placed_at"))
    filled = _market_time(order.get("filled_at"))
    exited = _market_time(order.get("exited_at"))

    fill_price = order.get("fill_price")
    limit_price = order.get("limit_price")
    quantity = order.get("quantity")

    # Negative means it filled BELOW the limit, which is the good direction
    # for a buy -- it says the buffer was wider than it needed to be.
    slippage = (
        round(fill_price - limit_price, 4)
        if fill_price is not None and limit_price is not None
        else None
    )

    cash_in = (
        round(fill_price * quantity * CONTRACT_MULTIPLIER, 2)
        if fill_price is not None and quantity is not None
        else None
    )

    held = order.get("held_seconds")
    if held is None:
        held = order.get("held_open_seconds")

    return [
        placed.date() if placed else None,
        placed.strftime("%H:%M:%S") if placed else None,
        placed.strftime("%A") if placed else None,
        placed.hour if placed else None,
        order.get("underlying"),
        order.get("strike"),
        order.get("option_type"),
        order.get("expiry"),
        quantity,
        limit_price,
        fill_price,
        slippage,
        order.get("exit_price"),
        order.get("outcome"),
        order.get("realised_pnl"),
        order.get("realised_pnl_percent"),
        cash_in,
        order.get("take_profit_price"),
        order.get("stop_loss_price"),
        _percent_away(fill_price, order.get("take_profit_price")),
        _percent_away(fill_price, order.get("stop_loss_price")),
        held,
        round(held / 60, 2) if held is not None else None,
        order.get("fill_delay_seconds"),
        filled.strftime("%H:%M:%S") if filled else None,
        exited.strftime("%H:%M:%S") if exited else None,
        order.get("leg_time_in_force"),
        order.get("identifier"),
        order.get("order_id_text"),
        order.get("outcome_note"),
    ]


def build_leg_rows(order: dict) -> list[list]:
    """Flatten one order's bracket legs into their own rows.

    Kept on a separate sheet rather than widened onto the order row: an order
    has two legs, and repeating every order column twice to hold them would
    make both harder to read.

    Args:
        order: One entry from GET /orders/history.

    Returns:
        A list of rows, in LEG_COLUMNS order. Empty when there are no legs.
    """
    placed = _market_time(order.get("placed_at"))

    return [
        [
            placed.date() if placed else None,
            order.get("underlying"),
            order.get("strike"),
            order.get("option_type"),
            leg.get("role"),
            leg.get("order_type"),
            leg.get("price"),
            leg.get("time_in_force"),
            leg.get("status"),
            order.get("outcome"),
            order.get("identifier"),
            leg.get("order_id_text"),
        ]
        for leg in order.get("legs") or []
    ]
