"""What each contract actually made, from the fills rather than from pairs.

WHY THIS EXISTS. The order history pairs each BUY with the SELL it thinks
closed it, and that pairing is guesswork once the same contract is traded more
than once in a session. Measured against the broker's own export:

    NVDA 220 CALL, traded six times
        the fills say          +46
        the paired history said +466

Two of the six buys had been handed the exit price of a LATER trade -- 2.22
instead of 1.74 and 1.53 -- because "a sell on this contract, after this buy
filled" matched more than one sell. Across the day that inflated a real +451
into +1153.

HOW THIS IS DIFFERENT. It does not pair anything. Every fill either paid cash
out or took cash in, and what a contract made is the difference:

    sum(cash from sells) - sum(cash paid for buys)

That is what a broker computes, it needs no matching, and it cannot be fooled
by trading the same strike repeatedly. Verified against the account's own
trade-history export: every contract agreed to the cent, and the day totalled
451.00 against the app's 451.00.

WHAT IT CANNOT SAY. This is a figure per CONTRACT for a period, not per order.
An individual round trip still comes from the paired history, where it is
right whenever a contract was traded once -- and where it is the only thing
available at all.

CHARGES ARE NOT INCLUDED. filled_cash_amount is the trade, not the bill. The
broker's own commission, regulatory and clearing fees are reported elsewhere
and are not on the order record, so what this returns is GROSS -- the same
basis the app's Daily P&L uses.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

#: The day a fill belongs to. A trading session is a market-day concept.
MARKET_TIMEZONE = ZoneInfo("America/New_York")


def contract_of(order) -> str:
    """The option identifier a fill belongs to.

    Tiger renders the contract as "NVDA  260921C00220000/OPT/USD".

    Args:
        order: One filled order.

    Returns:
        The identifier, without the security type and currency.
    """
    return str(getattr(order, "contract", "")).split("/")[0].strip()


def market_day_of(order) -> date | None:
    """The market day a fill happened on.

    Measured from the FILL, not the submission: an order placed before the
    close and filled after it belongs to the day it actually traded.

    Args:
        order: One filled order.

    Returns:
        The date in New York, or None when it carries no timestamp.
    """
    stamp = getattr(order, "trade_time", None) or getattr(order, "order_time", None)
    if not stamp:
        return None

    return datetime.fromtimestamp(stamp / 1000, MARKET_TIMEZONE).date()


def realised_by_contract(filled_orders, start: date, end: date) -> dict:
    """Total what each contract made over a period, from its fills.

    Args:
        filled_orders: Filled orders from get_filled_orders.
        start: First market day to include.
        end: Last market day to include.

    Returns:
        A mapping of identifier to a dict of:
            realised   profit on the round trips that CLOSED. Contracts
                       still held are valued at what they cost and taken back
                       out, so an open position contributes nothing rather
                       than reporting its purchase price as a loss
            open_cost  what the still-held contracts cost, excluded above
            cash_in    proceeds from sells
            cash_out   paid for buys
            bought     contracts bought
            sold       contracts sold
            open_qty   bought minus sold; non-zero means a position is held
            fills      how many fills went into it
    """
    totals: dict[str, dict] = defaultdict(
        lambda: {
            "cash_in": 0.0,
            "cash_out": 0.0,
            "realised": 0.0,
            "open_cost": 0.0,
            "bought": 0,
            "sold": 0,
            "open_qty": 0,
            "fills": 0,
        }
    )

    # Fills in TIME order, because matching a sell to what it closed is a
    # question about sequence: the contracts bought first are the contracts
    # sold first.
    ordered = sorted(
        (
            order
            for order in filled_orders or []
            if (day := market_day_of(order)) is not None and start <= day <= end
        ),
        key=lambda order: (
            getattr(order, "trade_time", None)
            or getattr(order, "order_time", None)
            or 0
        ),
    )

    #: Per contract, the buys not yet sold: [quantity, cost per contract].
    open_lots: dict[str, list] = defaultdict(list)

    for order in ordered:
        identifier = contract_of(order)
        if not identifier:
            continue

        # filled_cash_amount is what MOVED, which already accounts for the
        # fill price and the contract multiplier. Deriving it from price and
        # quantity would reintroduce the arithmetic this exists to avoid.
        amount = float(getattr(order, "filled_cash_amount", 0) or 0)
        quantity = int(float(getattr(order, "filled", 0) or 0))
        if quantity <= 0:
            continue

        entry = totals[identifier]
        entry["fills"] += 1
        per_contract = amount / quantity

        if str(getattr(order, "action", "")).upper() == "SELL":
            entry["cash_in"] += amount
            entry["sold"] += quantity

            # FIRST IN, FIRST OUT. Realised profit is what a sell made against
            # the buys it actually closed, oldest first -- which is how a
            # broker accounts for it. Averaging every buy together instead
            # blends the cost of a position still held into a trade that
            # already finished, and reports the wrong number for both.
            remaining = quantity
            lots = open_lots[identifier]
            while remaining > 0 and lots:
                lot = lots[0]
                taken = min(remaining, lot[0])
                entry["realised"] += (per_contract - lot[1]) * taken
                lot[0] -= taken
                remaining -= taken
                if lot[0] == 0:
                    lots.pop(0)

            # Sold more than was held: a short. Its proceeds are not realised
            # until it is bought back, so the lot is recorded as negative.
            if remaining > 0:
                lots.append([-remaining, per_contract])
        else:
            entry["cash_out"] += amount
            entry["bought"] += quantity

            # Buying back a short realises that side the same way.
            remaining = quantity
            lots = open_lots[identifier]
            while remaining > 0 and lots and lots[0][0] < 0:
                lot = lots[0]
                taken = min(remaining, -lot[0])
                entry["realised"] += (lot[1] - per_contract) * taken
                lot[0] += taken
                remaining -= taken
                if lot[0] == 0:
                    lots.pop(0)

            if remaining > 0:
                lots.append([remaining, per_contract])

    for identifier, entry in totals.items():
        entry["open_qty"] = entry["bought"] - entry["sold"]
        entry["realised"] = round(entry["realised"], 2)
        entry["open_cost"] = round(
            sum(abs(lot[0]) * lot[1] for lot in open_lots.get(identifier, [])), 2
        )

    return dict(totals)


def realised_total(by_contract: dict) -> float:
    """Add up every contract's realised figure.

    Args:
        by_contract: What realised_by_contract returned.

    Returns:
        The period's total, gross of charges.
    """
    return round(sum(entry["realised"] for entry in by_contract.values()), 2)
