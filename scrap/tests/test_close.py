"""POST /positions/close, and the quick-sell steps that price it.

The refusals matter more than the happy path here. A close sends a SELL, and
the three ways it can go wrong -- selling what is not held, selling more than
is held, and selling at a price that is not a price -- each produce a real
order if they are not caught.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.errors import ApiError  # noqa: E402
from api.routes.close import find_position, resolve_sell_limit  # noqa: E402
from api.service.core.config import ConfigError, _get_quick_sell_steps  # noqa: E402


class FakePosition:
    """Enough of an OptionPosition for the lookup and the guards."""

    def __init__(self, identifier, quantity=5.0):
        self.identifier = identifier
        self.quantity = quantity
        self.underlying = identifier.split()[0]
        self.strike = 215.0
        self.put_call = "CALL"
        self.expiry_date_text = "2026-09-14"
        self.expiry_compact = "20260914"


class TestFindPosition:
    """Closing something that is not held must never become a short sale."""

    def test_a_held_position_is_found(self, monkeypatch):
        held = FakePosition("NVDA  260914C00215000")
        monkeypatch.setattr(
            "api.routes.close.list_option_positions", lambda _client: [held]
        )

        found = find_position("NVDA  260914C00215000", None)

        assert found is held

    def test_surrounding_whitespace_is_ignored(self, monkeypatch):
        held = FakePosition("NVDA  260914C00215000")
        monkeypatch.setattr(
            "api.routes.close.list_option_positions", lambda _client: [held]
        )

        assert find_position("  NVDA  260914C00215000  ", None) is held

    def test_the_inner_spacing_must_match_exactly(self, monkeypatch):
        """Tiger's identifiers carry meaningful padding -- one space is a
        different contract from two, so a near miss is refused rather than
        matched to whatever looks close."""
        monkeypatch.setattr(
            "api.routes.close.list_option_positions",
            lambda _client: [FakePosition("NVDA  260914C00215000")],
        )

        with pytest.raises(ApiError) as caught:
            find_position("NVDA 260914C00215000", None)

        assert caught.value.status_code == 404
        assert caught.value.error_code == "POSITION_NOT_HELD"

    def test_nothing_held_is_refused_not_sold_short(self, monkeypatch):
        monkeypatch.setattr(
            "api.routes.close.list_option_positions", lambda _client: []
        )

        with pytest.raises(ApiError) as caught:
            find_position("NVDA  260914C00215000", None)

        assert caught.value.status_code == 404

    def test_a_different_contract_does_not_match(self, monkeypatch):
        """Same underlying, different strike. Selling the wrong contract is
        worse than selling nothing."""
        monkeypatch.setattr(
            "api.routes.close.list_option_positions",
            lambda _client: [FakePosition("NVDA  260914C00220000")],
        )

        with pytest.raises(ApiError):
            find_position("NVDA  260914C00215000", None)


class TestResolveSellLimit:
    """A sell limit is snapped DOWN, and must still be a price."""

    def test_a_price_already_on_the_grid_is_unchanged(self):
        assert resolve_sell_limit(0.05, 0.01) == pytest.approx(0.05)

    def test_an_off_grid_price_snaps_down_not_to_nearest(self):
        """0.058 is nearer 0.06, but rounding a SELL limit up asks for more
        than the caller chose and risks not filling."""
        assert resolve_sell_limit(0.058, 0.01) == pytest.approx(0.05)

    def test_a_step_that_falls_through_zero_is_refused(self):
        """0.10 below a 0.02 premium. The quick-sell buttons can produce this
        on a cheap option, and it is not a price."""
        with pytest.raises(ApiError) as caught:
            resolve_sell_limit(-0.08, 0.01)

        assert caught.value.status_code == 422
        assert caught.value.error_code == "SELL_PRICE_NOT_POSITIVE"

    def test_exactly_zero_is_refused(self):
        with pytest.raises(ApiError):
            resolve_sell_limit(0.0, 0.01)

    def test_a_price_below_one_tick_is_refused(self):
        """0.004 snaps down to 0.00, which is not a price even though the
        input was positive."""
        with pytest.raises(ApiError) as caught:
            resolve_sell_limit(0.004, 0.01)

        assert caught.value.error_code == "SELL_PRICE_NOT_POSITIVE"


class TestQuickSellSteps:
    """The steps are dollars BELOW the premium, so each must be positive."""

    def test_a_list_is_parsed_and_sorted(self, monkeypatch):
        monkeypatch.setenv("QUICK_SELL_STEPS", "0.05, 0.01, 0.10, 0.02")

        assert _get_quick_sell_steps("QUICK_SELL_STEPS", ()) == (
            0.01, 0.02, 0.05, 0.10,
        )

    def test_absent_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.delenv("QUICK_SELL_STEPS", raising=False)

        assert _get_quick_sell_steps("QUICK_SELL_STEPS", (0.01, 0.02)) == (0.01, 0.02)

    def test_zero_is_refused(self, monkeypatch):
        """A zero step offers to sell AT the premium, which is not an exit."""
        monkeypatch.setenv("QUICK_SELL_STEPS", "0.01, 0")

        with pytest.raises(ConfigError, match="greater than zero"):
            _get_quick_sell_steps("QUICK_SELL_STEPS", ())

    def test_a_negative_step_is_refused(self, monkeypatch):
        """Negative would price ABOVE the premium -- the opposite of a quick
        exit, and it would sit on the book unfilled."""
        monkeypatch.setenv("QUICK_SELL_STEPS", "-0.01")

        with pytest.raises(ConfigError, match="greater than zero"):
            _get_quick_sell_steps("QUICK_SELL_STEPS", ())

    def test_a_non_number_is_refused(self, monkeypatch):
        monkeypatch.setenv("QUICK_SELL_STEPS", "0.01, cheap")

        with pytest.raises(ConfigError, match="not a number"):
            _get_quick_sell_steps("QUICK_SELL_STEPS", ())

    def test_blank_entries_are_skipped(self, monkeypatch):
        """A trailing comma is a typo, not a reason to refuse to start."""
        monkeypatch.setenv("QUICK_SELL_STEPS", "0.01, 0.02,")

        assert _get_quick_sell_steps("QUICK_SELL_STEPS", ()) == (0.01, 0.02)


class TestNothingFailsAfterSubmission:
    """The bug this endpoint was rebuilt to avoid.

    An exception after place_order returns 500 for an order that is already
    live, and a caller who retries sells the position twice. It happened once:
    the response read position.underlying from an object that did not have it,
    AFTER the sell had filled.

    The defence is structural -- every lookup happens before submission -- so
    the test is structural too.
    """

    def source(self) -> str:
        return (PROJECT_ROOT / "api/routes/close.py").read_text(encoding="utf-8")

    def test_the_response_is_built_only_from_locals(self):
        """No attribute access on `position` or `contract` after the sell.

        Both are read into plain locals before submission precisely so that
        building the response cannot raise.
        """
        source = self.source()
        after_submission = source.split("outcome, estimate = sell_option(")[1]
        response_block = after_submission.split("return ClosePositionResponse(")[1]

        assert "position." not in response_block
        assert "contract." not in response_block

    def test_the_guards_run_before_the_sell(self):
        source = self.source()
        sell_at = source.index("outcome, estimate = sell_option(")

        for guard in ("find_position(", "resolve_sell_limit(", "QUANTITY_EXCEEDS_POSITION"):
            assert source.index(guard) < sell_at, f"{guard} must run before the sell"
