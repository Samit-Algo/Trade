"""Everything about an order, from arithmetic to the wire.

    cost.py       What will this cost me?          pure maths, no network
    build.py      Build it, and show it to a human  builds nothing live
    bracket.py    Take-profit and stop-loss legs
    status.py  Status, fills, cancelling  <- cancel_order lives here
    submit.py     THE ONLY FILE THAT CAN SPEND MONEY

Read `submit.py` if you are reviewing safety. Every `place_order` call in the
project is in it, each between two `assert_order_allowed` gates.
"""

from __future__ import annotations

from .cost import (
    BUY, SELL, VALID_ACTIONS, PricingError, CostEstimate, validate_action,
    validate_quantity, choose_price, calculate_total_cash,
    calculate_break_even, calculate_maximum_loss, estimate_cost,
    normalise_limit_price, compare_to_available_cash
)
from .build import (
    RULE_WIDTH, DEFAULT_TIME_IN_FORCE, build_option_order, format_money,
    print_manual_data_banner, print_order_preview, simulate_order
)
from .ticks import (
    MEASURED_TICK_SIZE, TICK_SOURCE_NOTE, TickError, apply_buffer, is_on_tick,
    snap_down, snap_nearest, snap_up,
)
from .bracket import (
    COMMISSION_BASE, COMMISSION_PER_CONTRACT, LEG_PROFIT, LEG_LOSS,
    BracketCalculation, BracketError, BracketLegs,
    calculate_bracket_from_percentages, estimate_commission_per_order,
    estimate_commission_per_share, estimate_round_trip_commission,
    is_take_profit_a_losing_exit, validate_bracket_prices,
    calculate_intended_risk, build_option_order_with_bracket,
    print_bracket_preview, get_attached_legs, print_attached_legs
)
from .status import (
    DEFAULT_POLL_ATTEMPTS, DEFAULT_POLL_DELAY_SECONDS, NOTHING_FILLED,
    PARTIALLY_FILLED, FULLY_FILLED, TERMINAL_STATUSES, FillOutcome,
    OrderSubmissionError, normalise_status, is_terminal_status,
    classify_fill, calculate_actual_cash, get_order_status,
    poll_until_settled, print_fill_outcome, cancel_order
)
from .submit import (
    build_cash_confirmation_phrase, confirm_cash_amount, buy_option,
    sell_option, buy_option_with_bracket
)

__all__ = [
    "MEASURED_TICK_SIZE", "TICK_SOURCE_NOTE", "TickError", "snap_up", "snap_down",
    "snap_nearest", "apply_buffer", "is_on_tick", "BracketCalculation",
    "calculate_bracket_from_percentages",
    "BUY", "SELL", "VALID_ACTIONS", "PricingError", "CostEstimate",
    "validate_action", "validate_quantity", "choose_price",
    "calculate_total_cash", "calculate_break_even",
    "calculate_maximum_loss", "estimate_cost", "normalise_limit_price",
    "compare_to_available_cash", "RULE_WIDTH", "DEFAULT_TIME_IN_FORCE",
    "build_option_order", "format_money", "print_manual_data_banner",
    "print_order_preview", "simulate_order", "COMMISSION_BASE",
    "COMMISSION_PER_CONTRACT", "LEG_PROFIT", "LEG_LOSS", "BracketError",
    "BracketLegs", "estimate_commission_per_order",
    "estimate_commission_per_share", "estimate_round_trip_commission",
    "is_take_profit_a_losing_exit", "validate_bracket_prices",
    "calculate_intended_risk", "build_option_order_with_bracket",
    "print_bracket_preview", "get_attached_legs", "print_attached_legs",
    "DEFAULT_POLL_ATTEMPTS", "DEFAULT_POLL_DELAY_SECONDS", "NOTHING_FILLED",
    "PARTIALLY_FILLED", "FULLY_FILLED", "TERMINAL_STATUSES", "FillOutcome",
    "OrderSubmissionError", "normalise_status", "is_terminal_status",
    "classify_fill", "calculate_actual_cash", "get_order_status",
    "poll_until_settled", "print_fill_outcome", "cancel_order",
    "build_cash_confirmation_phrase", "confirm_cash_amount", "buy_option",
    "sell_option", "buy_option_with_bracket"
]
