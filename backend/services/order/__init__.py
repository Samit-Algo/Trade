"""Everything about an order that is arithmetic rather than a broker call.

    pricing_rules.py  the tick grid, buffer tiers, quantity tiers
    build.py          what an order will cost -- pure maths, no network
    bracket.py        the take-profit and stop-loss prices, and commission
    status.py         what a fill means: filled, part-filled, nothing
    journey.py        what a contract did between entry and exit
    symbol_lock.py    one trade per underlying at a time

Building, sending and cancelling a real order is the market's job. For US
options that is backend/markets/us/ -- and its submit.py is the only file in
the project that can spend money.
"""

from __future__ import annotations

from .pricing_rules import (
    MEASURED_TICK_SIZE, TICK_SOURCE_NOTE, TickError, apply_buffer, is_on_tick,
    snap_down, snap_nearest, snap_up, resolve_buffer_ticks, resolve_quantity,
)
from .build import (
    BUY, SELL, VALID_ACTIONS, PricingError, CostEstimate, validate_action,
    validate_quantity, choose_price, calculate_total_cash,
    calculate_break_even, calculate_maximum_loss, estimate_cost,
    normalise_limit_price, compare_to_available_cash,
    RULE_WIDTH, DEFAULT_TIME_IN_FORCE, format_money,
)
from .bracket import (
    COMMISSION_BASE, COMMISSION_PER_CONTRACT,
    BracketCalculation, BracketError, BracketLegs,
    calculate_bracket_from_percentages, estimate_commission_per_order,
    estimate_commission_per_share, estimate_round_trip_commission,
    is_take_profit_a_losing_exit, validate_bracket_prices,
    calculate_intended_risk,
)
from .status import (
    DEFAULT_POLL_ATTEMPTS, DEFAULT_POLL_DELAY_SECONDS, NOTHING_FILLED,
    PARTIALLY_FILLED, FULLY_FILLED, TERMINAL_STATUSES, FillOutcome,
    OrderSubmissionError, normalise_status, is_terminal_status,
    classify_fill, calculate_actual_cash,
)

__all__ = [
    "MEASURED_TICK_SIZE", "TICK_SOURCE_NOTE", "TickError", "apply_buffer",
    "is_on_tick", "snap_down", "snap_nearest", "snap_up",
    "resolve_buffer_ticks", "resolve_quantity",
    "BUY", "SELL", "VALID_ACTIONS", "PricingError", "CostEstimate",
    "validate_action", "validate_quantity", "choose_price",
    "calculate_total_cash", "calculate_break_even",
    "calculate_maximum_loss", "estimate_cost", "normalise_limit_price",
    "compare_to_available_cash", "RULE_WIDTH", "DEFAULT_TIME_IN_FORCE",
    "format_money",
    "COMMISSION_BASE", "COMMISSION_PER_CONTRACT", "BracketCalculation",
    "BracketError", "BracketLegs", "calculate_bracket_from_percentages",
    "estimate_commission_per_order", "estimate_commission_per_share",
    "estimate_round_trip_commission", "is_take_profit_a_losing_exit",
    "validate_bracket_prices", "calculate_intended_risk",
    "DEFAULT_POLL_ATTEMPTS", "DEFAULT_POLL_DELAY_SECONDS", "NOTHING_FILLED",
    "PARTIALLY_FILLED", "FULLY_FILLED", "TERMINAL_STATUSES", "FillOutcome",
    "OrderSubmissionError", "normalise_status", "is_terminal_status",
    "classify_fill", "calculate_actual_cash",
]
