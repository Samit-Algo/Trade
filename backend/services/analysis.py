"""The Analysis page: a range of trades, taken apart simply.

Three answers, nothing more:

    results      how many trades, how many took profit, how many stopped out,
                 and the money
    heat map     money by 15-minute slot after the open, and by symbol
    stop losses  why each one fired, as one of three kinds

PURE. History rows and recorded price paths go in; a plain dict comes out.
Nothing here reaches a broker. Figures are gross -- no commission, for either
market.

THE THREE KINDS OF STOP LOSS, read from the prices recorded while it was held:

    A  wrong from the start   never reached +3%, stopped inside 5 minutes
                              -> the ENTRY is the problem
    B  was winning, reversed  reached +3% or more first
                              -> the EXIT is the problem
    C  slow drop              never reached +3%, took 5 minutes or more
                              -> the trade needs a time limit

A stop with no recorded prices -- held before recording began -- is listed
but not given a kind: guessing would be worse than saying so.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta

#: A stop counts as "was winning first" (kind B) from this gain, in percent.
WINNING_FIRST_PERCENT = 3.0

#: A stop that took at least this long is a slow drop (kind C).
SLOW_DROP_SECONDS = 300

#: The heat map's time slots, in minutes.
SLOT_MINUTES = 15

ADVICE = {
    "A": "Most stops never went up at all -- the ENTRY is the problem: the "
         "signal buys late or in the wrong direction. A different stop will "
         "not fix that.",
    "B": "Most stops were up +{t:g}% or more first, then reversed -- the EXIT is "
         "the problem: a nearer take profit, or moving the stop to break-even "
         "once in profit, would have kept some of it.",
    "C": "Most stops fell slowly over 5 minutes or more -- a time limit on "
         "each trade would close these sooner.",
}


def _moment(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value) -> float:
    return round(float(value or 0), 2)


def multiplier_of(row: dict, fallback: float) -> float:
    """Units per contract, recovered from the row's own realised P&L.

    Price, quantity and money are all on the row, so the multiplier is what
    makes them agree: 100 for a US option, the lot for NIFTY.
    """
    pnl, entry, exit_price = row.get("realised_pnl"), row.get("fill_price"), row.get("exit_price")
    quantity = row.get("quantity") or 0
    if pnl and entry and exit_price and quantity and exit_price != entry:
        found = abs(pnl / ((exit_price - entry) * quantity))
        if found >= 0.5:
            return float(round(found))
    return float(fallback)


def slot_of(moment: datetime, timezone, opens: time) -> tuple[int, str]:
    """The 15-minute slot after the open a moment falls in, and its label."""
    local = moment.astimezone(timezone)
    opened = datetime.combine(local.date(), opens, tzinfo=timezone)
    index = int((local - opened).total_seconds() // 60) // SLOT_MINUTES
    start = opened + timedelta(minutes=index * SLOT_MINUTES)
    return index, f"{start:%H:%M} - {start + timedelta(minutes=SLOT_MINUTES):%H:%M}"


def results(rows: list[dict]) -> dict:
    """The pie chart: how the trades ended, and the money."""
    closed = [r for r in rows if r.get("realised_pnl") is not None]
    count = defaultdict(int)
    for r in rows:
        count[r.get("outcome")] += 1
    open_ = count["STILL_OPEN"]
    return {
        "total": len(rows),
        "take_profit": count["TOOK_PROFIT"],
        "stop_loss": count["STOPPED_OUT"],
        "still_open": open_,
        "other": len(rows) - count["TOOK_PROFIT"] - count["STOPPED_OUT"] - open_,
        "pnl": _money(sum(r["realised_pnl"] for r in closed)),
        "wins": sum(1 for r in closed if r["realised_pnl"] > 0),
        "closed": len(closed),
    }


def heatmap(rows: list[dict], timezone, opens: time) -> dict:
    """Money by 15-minute slot after the open, and by symbol."""
    cells: dict[tuple[int, str], list] = defaultdict(list)
    labels: dict[int, str] = {}
    symbols: list[str] = []
    for r in rows:
        moment = _moment(r.get("placed_at"))
        if moment is None:
            continue
        index, label = slot_of(moment, timezone, opens)
        labels[index] = label
        symbol = r.get("underlying") or "?"
        if symbol not in symbols:
            symbols.append(symbol)
        cells[(index, symbol)].append(r)

    def cell(group):
        if not group:
            return None
        closed = [r for r in group if r.get("realised_pnl") is not None]
        return {"pnl": _money(sum(r["realised_pnl"] for r in closed)),
                "trades": len(group),
                "wins": sum(1 for r in closed if r["realised_pnl"] > 0)}

    return {
        "symbols": symbols,
        "rows": [{"slot": labels[i], "cells": [cell(cells.get((i, s))) for s in symbols]}
                 for i in sorted(labels)],
    }


def stop_losses(rows: list[dict], paths: dict[str, list[tuple[int, float]]],
                fallback_multiplier: float, threshold: float = WINNING_FIRST_PERCENT) -> dict:
    """Every stopped-out trade, as kind A, B or C, and what that says."""
    kinds = {k: {"count": 0, "pnl": 0.0} for k in "ABC"}
    listed, gave_back = [], 0.0

    for r in rows:
        if r.get("outcome") != "STOPPED_OUT":
            continue
        entry = r.get("fill_price")
        prices = [p for _, p in paths.get(r.get("order_id_text"), [])]
        best = max(prices) if entry and len(prices) >= 2 else None
        best_percent = round((best / entry - 1) * 100, 2) if best else None

        seconds = r.get("held_seconds")
        filled, exited = _moment(r.get("filled_at")), _moment(r.get("exited_at"))
        if seconds is None and filled and exited:
            seconds = (exited - filled).total_seconds()

        if best_percent is None:
            kind = None
        elif best_percent >= threshold:
            kind = "B"
        elif seconds is not None and seconds >= SLOW_DROP_SECONDS:
            kind = "C"
        else:
            kind = "A"

        given = None
        if kind:
            kinds[kind]["count"] += 1
            kinds[kind]["pnl"] += r.get("realised_pnl") or 0
        if kind == "B" and r.get("exit_price") is not None:
            given = _money((best - r["exit_price"]) * multiplier_of(r, fallback_multiplier)
                           * (r.get("quantity") or 0))
            gave_back += given

        listed.append({
            "order_id": r.get("order_id_text"), "placed_at": r.get("placed_at"),
            "symbol": r.get("underlying"), "strike": r.get("strike"),
            "side": r.get("option_type"), "entry": entry, "exit": r.get("exit_price"),
            "best_percent": best_percent, "seconds_to_stop": seconds, "kind": kind,
            "pnl": _money(r.get("realised_pnl")), "gave_back": given,
        })

    for k in kinds.values():
        k["pnl"] = _money(k["pnl"])
    typed = sum(k["count"] for k in kinds.values())
    top = max(kinds, key=lambda k: kinds[k]["count"]) if typed else None

    return {
        "threshold_percent": threshold,
        "total": len(listed),
        "kinds": kinds,
        "untyped": len(listed) - typed,
        "gave_back": _money(gave_back),
        "advice": ADVICE[top].format(t=threshold) if top else
                  "No stop losses with recorded prices in this range.",
        "trades": sorted(listed, key=lambda s: s["placed_at"] or "", reverse=True),
    }


def build(rows: list[dict], paths: dict[str, list[tuple[int, float]]], *,
          timezone, opens: time, fallback_multiplier: float) -> dict:
    """Everything the Analysis page shows, for trades that bought something."""
    bought = [r for r in rows if r.get("fill_price")]
    return {
        "results": results(bought),
        "heatmap": heatmap(bought, timezone, opens),
        "stop_losses": stop_losses(bought, paths, fallback_multiplier),
    }
