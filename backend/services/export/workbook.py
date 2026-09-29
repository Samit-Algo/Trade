"""Building the .xlsx itself.

Three sheets, because they answer different questions:

    Orders   one row per trade -- the sheet to sort, filter and pivot
    Summary  what the period did, so the file says something without work
    Legs     every bracket leg, for studying which exit actually fires

WRITTEN TO MEMORY, not to a file on disk. The caller streams it straight back
as a download; nothing is left behind on the machine running the service.

NOTHING IS ESTIMATED. Every figure comes from the broker's own record. There
is no commission column: it is not on the order record, and a number computed
from a rate would read as a measurement while being an assumption.
"""

from __future__ import annotations

import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .columns import LEG_COLUMNS, ORDER_COLUMNS, build_leg_rows, build_order_row
from .realised import realised_total

#: Dark enough for white text, and it survives printing in greyscale.
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)

#: The outcomes that count as a closed trade with a number attached. Used for
#: the win rate, which is meaningless over orders that never filled.
DECIDED_OUTCOMES = ("TOOK_PROFIT", "STOPPED_OUT", "CLOSED_MANUALLY", "CLOSED")


def _write_header(sheet, columns) -> None:
    """Write the heading row and size the columns.

    Args:
        sheet: The worksheet.
        columns: (heading, width) pairs.
    """
    for index, (heading, width) in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=heading)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = width

    # The headings stay visible while scrolling, and every column gets a
    # filter dropdown -- this sheet exists to be sliced.
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"


def _summary_rows(
    orders: list[dict], start: date, end: date, by_contract: dict | None = None
) -> list[tuple]:
    """Work out what the period did.

    Args:
        orders: The orders in the period.
        start: First day included.
        end: Last day included.

    Returns:
        (label, value) pairs.
    """
    decided = [o for o in orders if o.get("outcome") in DECIDED_OUTCOMES]
    with_pnl = [o for o in decided if o.get("realised_pnl") is not None]

    wins = [o for o in with_pnl if o["realised_pnl"] > 0]
    losses = [o for o in with_pnl if o["realised_pnl"] < 0]

    held = [
        o.get("held_seconds") for o in orders if o.get("held_seconds") is not None
    ]

    def total(rows):
        return round(sum(o["realised_pnl"] for o in rows), 2)

    return [
        ("Period", f"{start} to {end}" if start != end else str(start)),
        ("Orders placed", len(orders)),
        ("Trades decided", len(decided)),
        ("", ""),
        ("Take profit", sum(1 for o in orders if o.get("outcome") == "TOOK_PROFIT")),
        ("Stopped out", sum(1 for o in orders if o.get("outcome") == "STOPPED_OUT")),
        ("Closed by hand", sum(1 for o in orders if o.get("outcome") == "CLOSED_MANUALLY")),
        ("Closed, unmatched", sum(1 for o in orders if o.get("outcome") == "CLOSED")),
        ("Still open", sum(1 for o in orders if o.get("outcome") == "STILL_OPEN")),
        ("Not filled", sum(1 for o in orders if o.get("outcome") == "NOT_FILLED")),
        ("Cancelled", sum(1 for o in orders if o.get("outcome") == "CANCELLED")),
        ("", ""),
        ("Winners", len(wins)),
        ("Losers", len(losses)),
        (
            "Win rate %",
            round(len(wins) / len(with_pnl) * 100, 1) if with_pnl else None,
        ),
        ("", ""),
        # From the FILLS when available: cash in from sells minus cash out for
        # buys, which is what the broker computes and what its Daily P&L
        # shows. The per-order figures below it are the paired history, right
        # for a contract traded once and inflated for one traded repeatedly.
        (
            "Total P&L (from fills)",
            realised_total(by_contract) if by_contract else None,
        ),
        ("Total P&L (paired rows)", total(with_pnl) if with_pnl else None),
        ("Won", total(wins) if wins else None),
        ("Lost", total(losses) if losses else None),
        (
            "Average win",
            round(total(wins) / len(wins), 2) if wins else None,
        ),
        (
            "Average loss",
            round(total(losses) / len(losses), 2) if losses else None,
        ),
        ("Best", max((o["realised_pnl"] for o in with_pnl), default=None)),
        ("Worst", min((o["realised_pnl"] for o in with_pnl), default=None)),
        ("", ""),
        (
            "Average hold (min)",
            round(sum(held) / len(held) / 60, 2) if held else None,
        ),
        ("Shortest hold (s)", round(min(held), 1) if held else None),
        ("Longest hold (s)", round(max(held), 1) if held else None),
        ("", ""),
        (
            "Note",
            "Total P&L (from fills) is the broker's own basis: cash in minus "
            "cash out, gross of commission. Use it. The paired-rows figure "
            "above it pairs each buy with a sell, which over-counts when one "
            "contract is traded several times in a session -- it is kept only "
            "so a single trade's row can be read.",
        ),
    ]


def build_workbook(
    orders: list[dict], start: date, end: date, by_contract: dict | None = None
) -> bytes:
    """Build the spreadsheet for a period.

    Args:
        orders: Orders from GET /orders/history, already filtered to the
            period and sorted newest first.
        start: First day included.
        end: Last day included.
        by_contract: What each contract made, from the FILLS -- see
            realised.py. The per-order P&L in `orders` is pairing guesswork
            once a contract is traded more than once in a session, so this is
            what the totals are built from when it is supplied.

    Returns:
        The .xlsx file, as bytes ready to stream.
    """
    book = Workbook()

    # --- Orders: the sheet this file exists for -------------------------
    sheet = book.active
    sheet.title = "Orders"
    _write_header(sheet, ORDER_COLUMNS)

    # Oldest first: a spreadsheet is read downwards, and a session reads
    # forwards. The page shows newest first because a screen is read from the
    # top and the newest order is the one being watched.
    for order in reversed(orders):
        sheet.append(build_order_row(order))

    # --- Summary --------------------------------------------------------
    summary = book.create_sheet("Summary")
    summary.column_dimensions["A"].width = 22
    summary.column_dimensions["B"].width = 62

    for label, value in _summary_rows(orders, start, end, by_contract):
        summary.append((label, value))
        if label:
            summary.cell(row=summary.max_row, column=1).font = Font(bold=True)

    # The note at the bottom is a sentence, not a number.
    summary.cell(row=summary.max_row, column=2).alignment = Alignment(
        horizontal="left", wrap_text=True
    )

    # --- By contract: the figures that are actually right ---------------
    if by_contract:
        sheet = book.create_sheet("By contract")
        _write_header(sheet, (
            ("Contract", 26),
            ("Realised", 12),
            ("Bought", 10),
            ("Sold", 10),
            ("Still open", 11),
            ("Fills", 8),
        ))
        for identifier in sorted(by_contract):
            entry = by_contract[identifier]
            sheet.append([
                identifier,
                entry["realised"],
                entry["bought"],
                entry["sold"],
                entry["open_qty"] or None,
                entry["fills"],
            ])

    # --- Legs -----------------------------------------------------------
    legs_sheet = book.create_sheet("Legs")
    _write_header(legs_sheet, LEG_COLUMNS)

    for order in reversed(orders):
        for leg_row in build_leg_rows(order):
            legs_sheet.append(leg_row)

    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()
