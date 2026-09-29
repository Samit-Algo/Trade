"""Reading OpenAlgo's order book, trade book and positions, for India.

Everything here READS. Nothing here can place or cancel an order.

THREE THINGS THE BOOKS DO NOT SAY STRAIGHT, found in OpenAlgo's own code:

  1. The order book has no filled quantity for Angel, and its "price" is the
     average fill once an order completes but the limit while it rests. So
     what filled, and at what, is read from the TRADE book -- one row per
     fill, the same shape live and in Analyze mode.
  2. Angel's books hold TODAY only. The history page shows today's Indian
     orders; older ones are in logs/order_audit.log.
  3. Quantities are in UNITS (65 for one NIFTY lot). This service counts
     LOTS, as the US counts contracts, so every quantity here is divided by
     the lot and the lot is the multiplier: premium x lot x lots is cash.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from backend.services.position import OptionPosition

from ..base import BrokerOrder
from .contract import compact_expiry, parse_identifier
from .openalgo import unwrap

IST = timezone(timedelta(hours=5, minutes=30))

#: OpenAlgo's order statuses (Angel's, lower case) as the upper-case names
#: the history and the page already read.
STATUS = {
    "complete": "FILLED",
    "open": "SUBMITTED",
    "trigger pending": "TRIGGER_PENDING",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "after market order req received": "SUBMITTED",
    "open pending": "SUBMITTED",
    "validation pending": "SUBMITTED",
    "put order req received": "SUBMITTED",
    "modified": "SUBMITTED",
    "modify pending": "SUBMITTED",
    "cancel pending": "SUBMITTED",
}

#: Still able to fill.
WORKING = {"SUBMITTED", "TRIGGER_PENDING"}

#: OpenAlgo's price types as the order types the history reads: anything
#: with "STP" in it is a stop loss.
ORDER_TYPE = {"LIMIT": "LMT", "MARKET": "MKT", "SL": "STP_LMT", "SL-M": "STP"}


def parse_time_ms(text) -> int | None:
    """An OpenAlgo timestamp, as epoch milliseconds. Both spellings are IST."""
    if not text:
        return None
    for layout in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            moment = datetime.strptime(str(text).strip(), layout).replace(tzinfo=IST)
            return int(moment.timestamp() * 1000)
        except ValueError:
            continue
    return None


@dataclass
class Fills:
    """What one order actually did, from the trade book."""

    units: float = 0.0
    cash: float = 0.0
    last_ms: int | None = None

    @property
    def average(self) -> float | None:
        return round(self.cash / self.units, 4) if self.units else None


def read_rows(client, method: str, what: str) -> list[dict]:
    """One of the books, as a list of dicts, whatever shape it arrives in."""
    data = unwrap(getattr(client, method)(), what)
    if isinstance(data, dict):
        for key in ("orders", "trades", "positions", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def fills_by_order(trades: list[dict]) -> dict[str, Fills]:
    """Sum the trade book per order id."""
    fills: dict[str, Fills] = defaultdict(Fills)
    for trade in trades:
        order_id = str(trade.get("orderid") or "")
        if not order_id:
            continue
        units = abs(float(trade.get("quantity") or 0))
        price = float(trade.get("average_price") or trade.get("price") or 0)
        entry = fills[order_id]
        entry.units += units
        entry.cash += units * price
        stamp = parse_time_ms(trade.get("timestamp"))
        if stamp and (entry.last_ms is None or stamp > entry.last_ms):
            entry.last_ms = stamp
    return dict(fills)


def to_broker_order(row: dict, fills: dict[str, Fills], lot_size: float,
                    links: dict[str, dict]) -> BrokerOrder:
    """One order-book row as the neutral BrokerOrder.

    Args:
        row: The order-book row.
        fills: The trade book, summed per order.
        lot_size: Units in one lot of this contract.
        links: What this service knows about orders it placed: the entry an
            exit belongs to, and the limit it was sent at.
    """
    order_id = str(row.get("orderid") or "")
    done = fills.get(order_id, Fills())
    known = links.get(order_id, {})
    status = STATUS.get(str(row.get("order_status") or "").strip().lower(),
                        str(row.get("order_status") or "").upper() or "UNKNOWN")
    price_type = str(row.get("pricetype") or row.get("price_type") or "").upper()
    units = abs(float(row.get("quantity") or 0))
    lot = float(lot_size) or 1.0

    # The row's "price" is the average fill once complete, so the limit comes
    # from what was sent when this service sent it.
    limit = known.get("limit_price")
    if limit is None and status != "FILLED" and price_type in ("LIMIT", "SL"):
        limit = float(row.get("price") or 0) or None
    trigger = float(row.get("trigger_price") or 0) or None
    placed = parse_time_ms(row.get("timestamp"))

    # The role this service gave an exit outranks its price type: a stop that
    # had to be finished at MARKET is still a stop loss, not a take profit.
    order_type = ORDER_TYPE.get(price_type, price_type)
    if known.get("role") == "STOP_LOSS" and "STP" not in order_type:
        order_type = f"STP_{order_type}"

    return BrokerOrder(
        id=order_id,
        parent_id=known.get("parent_id"),
        identifier=str(row.get("symbol") or ""),
        underlying=_underlying(str(row.get("symbol") or "")),
        action=str(row.get("action") or "").upper(),
        status=status,
        order_type=order_type,
        quantity=units / lot,
        filled=done.units / lot,
        avg_fill_price=done.average,
        limit_price=limit,
        aux_price=trigger,
        time_in_force="DAY",
        order_time=known.get("placed_ms") or placed,
        trade_time=done.last_ms,
        reason=str(row.get("rejection_reason") or "") or None,
        filled_cash_amount=round(done.cash, 2) if done.units else None,
        multiplier=lot,
    )


def _underlying(symbol: str) -> str:
    try:
        return parse_identifier(symbol)[0]
    except ValueError:
        return symbol.strip().upper()


def to_option_position(row: dict, lot_size: float, today: date) -> OptionPosition | None:
    """One position-book row as the neutral OptionPosition, or None if not an option."""
    try:
        underlying, expiry_text, put_call, strike = parse_identifier(str(row.get("symbol") or ""))
    except ValueError:
        return None
    units = float(row.get("quantity") or 0)
    if not units:
        return None
    expiry = date.fromisoformat(expiry_text)
    lot = float(lot_size) or 1.0
    return OptionPosition(
        identifier=str(row["symbol"]),
        underlying=underlying,
        expiry_date_text=expiry_text,
        expiry_compact=compact_expiry(expiry),
        strike=strike,
        put_call=put_call,
        multiplier=lot,
        quantity=units / lot,
        average_cost=float(row.get("average_price") or 0),
        days_to_expiry=(expiry - today).days,
        market_price_latest=float(row.get("ltp") or 0) or None,
        tiger_unrealised_pnl=float(row.get("pnl") or 0),
    )
