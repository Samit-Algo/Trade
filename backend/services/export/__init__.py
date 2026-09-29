"""Turning order history into a spreadsheet, for studying it elsewhere.

Deliberately its own package. Nothing in the trading path imports it, and it
imports nothing that can place an order -- so this can grow columns, sheets
and formats without any of that touching the code that spends money.
"""

from __future__ import annotations

from .columns import ORDER_COLUMNS, LEG_COLUMNS, build_order_row, build_leg_rows
from .realised import realised_by_contract, realised_total
from .workbook import build_workbook

__all__ = [
    "ORDER_COLUMNS",
    "LEG_COLUMNS",
    "build_order_row",
    "build_leg_rows",
    "build_workbook",
    "realised_by_contract",
    "realised_total",
]
