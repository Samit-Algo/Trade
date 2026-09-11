"""Unit tests for Phase 5 order outcome logic. Offline -- nothing is submitted.

The rule under test throughout: an order ID confirms submission, not
execution, and an order whose status is not FILLED may still have filled in
part. Reality is the filled quantity.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.contract import OptionContractInfo  # noqa: E402
from api.service.order import (  # noqa: E402
    FULLY_FILLED,
    NOTHING_FILLED,
    PARTIALLY_FILLED,
    build_cash_confirmation_phrase,
    build_option_order,
    calculate_actual_cash,
    classify_fill,
    confirm_cash_amount,
    is_terminal_status,
    normalise_status,
    poll_until_settled,
)
from api.service.order import estimate_cost  # noqa: E402
from api.service.market import QuoteSnapshot, QuoteSource  # noqa: E402


def make_contract():
    return OptionContractInfo(
        identifier="AAPL  260918C00360000",
        underlying="AAPL",
        expiry_date_text="2026-09-18",
        expiry_compact="20260918",
        strike=360.0,
        put_call="CALL",
        multiplier=100.0,
        contract_id=1,
        days_to_expiry=15,
        name="Apple",
    )


def make_quote(limit_price=0.30):
    return QuoteSnapshot(
        bid=0.26, ask=0.30, volume=450,
        limit_price=limit_price, source=QuoteSource.MANUAL,
        captured_at=datetime.now(timezone.utc),
    )


class StubOrder:
    """An Order as the SDK returns it."""

    def __init__(self, status, filled, avg_fill_price=None, reason=""):
        self.status = status
        self.filled = filled
        self.avg_fill_price = avg_fill_price
        self.reason = reason


class StubTradeClient:
    """Returns a scripted sequence of order states, one per poll."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def get_order(self, account=None, id=None, order_id=None, **kwargs):
        self.calls += 1
        if not self.states:
            return None
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)


class TestNormaliseStatus:
    def test_enum_value_maps_back_to_its_name(self):
        """OrderStatus.REJECTED is the string 'Inactive'. Not obvious."""
        assert normalise_status("Inactive") == "REJECTED"

    def test_new_is_the_string_initial(self):
        assert normalise_status("Initial") == "NEW"

    def test_a_plain_name_passes_through(self):
        assert normalise_status("FILLED") == "FILLED"

    def test_none_is_unknown_not_a_crash(self):
        assert normalise_status(None) == "UNKNOWN"


class TestTerminalStatus:
    def test_the_four_terminal_states(self):
        for status in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED"):
            assert is_terminal_status(status) is True

    def test_working_states_are_not_terminal(self):
        for status in ("NEW", "HELD", "PARTIALLY_FILLED", "PENDING_NEW"):
            assert is_terminal_status(status) is False


class TestClassifyFill:
    def test_nothing_filled(self):
        assert classify_fill(requested_quantity=1, filled_quantity=0) == NOTHING_FILLED

    def test_partially_filled(self):
        assert classify_fill(requested_quantity=5, filled_quantity=2) == PARTIALLY_FILLED

    def test_fully_filled(self):
        assert classify_fill(requested_quantity=5, filled_quantity=5) == FULLY_FILLED

    def test_overfill_counts_as_full(self):
        assert classify_fill(requested_quantity=1, filled_quantity=2) == FULLY_FILLED

    def test_classification_ignores_status_entirely(self):
        """A CANCELLED order that part-filled is PARTIALLY FILLED, not nothing."""
        assert classify_fill(5, 2) == PARTIALLY_FILLED


class TestActualCash:
    def test_computed_from_fills_not_from_the_request(self):
        """Requested 5 at 0.30, filled 2 at 0.28 -> $56, not $150."""
        assert calculate_actual_cash(0.28, filled_quantity=2, multiplier=100) == 56.0

    def test_nothing_filled_means_no_cash(self):
        assert calculate_actual_cash(0.28, filled_quantity=0, multiplier=100) is None

    def test_no_average_price_means_no_cash(self):
        assert calculate_actual_cash(None, filled_quantity=2, multiplier=100) is None

    def test_rounded_to_cents(self):
        assert calculate_actual_cash(0.3333, 3, 100) == 99.99


class TestPollUntilSettled:
    def test_stops_on_a_terminal_status(self):
        client = StubTradeClient([StubOrder("FILLED", 1, 0.28)])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=1, contract_multiplier=100,
            poll_attempts=5, poll_delay_seconds=0, announce=False,
        )
        assert outcome.reached_terminal_status is True
        assert outcome.poll_attempts == 1
        assert outcome.outcome == FULLY_FILLED
        assert outcome.actual_cash == 28.0

    def test_keeps_polling_while_the_order_is_working(self):
        client = StubTradeClient([
            StubOrder("Initial", 0),
            StubOrder("Submitted", 0),
            StubOrder("Filled", 1, 0.28),
        ])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=1, contract_multiplier=100,
            poll_attempts=5, poll_delay_seconds=0, announce=False,
        )
        assert outcome.poll_attempts == 3
        assert outcome.outcome == FULLY_FILLED

    def test_a_cancelled_order_that_part_filled_is_reported_as_partial(self):
        """The rule that matters: status CANCELLED, but 2 contracts are held."""
        client = StubTradeClient([StubOrder("Cancelled", 2, 0.28)])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=5, contract_multiplier=100,
            poll_attempts=3, poll_delay_seconds=0, announce=False,
        )
        assert outcome.status == "CANCELLED"
        assert outcome.outcome == PARTIALLY_FILLED
        assert outcome.filled_quantity == 2
        assert outcome.actual_cash == 56.0

    def test_a_rejected_order_reports_nothing_filled(self):
        client = StubTradeClient([StubOrder("Inactive", 0, None, "no permission")])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=1, contract_multiplier=100,
            poll_attempts=3, poll_delay_seconds=0, announce=False,
        )
        assert outcome.status == "REJECTED"
        assert outcome.outcome == NOTHING_FILLED
        assert outcome.actual_cash is None
        assert "no permission" in outcome.reason

    def test_running_out_of_attempts_is_not_reported_as_settled(self):
        """Not success and not failure -- the order is still live."""
        client = StubTradeClient([StubOrder("Submitted", 0)])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=1, contract_multiplier=100,
            poll_attempts=3, poll_delay_seconds=0, announce=False,
        )
        assert outcome.reached_terminal_status is False
        assert outcome.is_settled is False
        assert outcome.poll_attempts == 3

    def test_a_broker_returning_nothing_does_not_crash(self):
        client = StubTradeClient([])
        outcome = poll_until_settled(
            client, order_id=1, requested_quantity=1, contract_multiplier=100,
            poll_attempts=2, poll_delay_seconds=0, announce=False,
        )
        assert outcome.status == "UNKNOWN"
        assert outcome.outcome == NOTHING_FILLED


class TestCashConfirmation:
    def make_estimate(self, quantity=1):
        return estimate_cost(
            make_contract(), action="BUY", quantity=quantity,
            bid=0.26, ask=0.30, limit_price=0.30,
        )

    def test_the_phrase_is_the_cash_amount(self):
        assert build_cash_confirmation_phrase(self.make_estimate()) == "30.00"

    def test_typing_the_exact_amount_confirms(self):
        assert confirm_cash_amount(self.make_estimate(), input_function=lambda _: "30.00") is True

    def test_yes_does_not_confirm(self):
        """A typed cash amount, not a yes. This is the whole point."""
        assert confirm_cash_amount(self.make_estimate(), input_function=lambda _: "y") is False

    def test_the_wrong_amount_does_not_confirm(self):
        assert confirm_cash_amount(self.make_estimate(), input_function=lambda _: "300.00") is False

    def test_currency_symbols_and_commas_are_tolerated(self):
        estimate = self.make_estimate(quantity=50)
        assert build_cash_confirmation_phrase(estimate) == "1500.00"
        assert confirm_cash_amount(estimate, input_function=lambda _: "$1,500.00") is True

    def test_abandoning_the_prompt_does_not_confirm(self):
        def raise_eof(_):
            raise EOFError

        assert confirm_cash_amount(self.make_estimate(), input_function=raise_eof) is False


class TestBuiltOrderShape:
    def test_defaults_are_day_and_no_extended_hours(self):
        class StubSettings:
            account = "20191106192858300"

        order = build_option_order(
            StubSettings(), make_contract(), action="BUY", quantity=1, limit_price=0.30
        )
        assert order.time_in_force == "DAY"
        assert order.outside_rth is False
        assert order.order_type == "LMT"
        assert order.limit_price == 0.30
        assert order.quantity == 1
