"""Where the time window sits in the bracket resolution chain.

THE GUARANTEE THIS FILE EXISTS FOR. A symbol somebody took the trouble to
tune keeps its bracket all session. The clock governs the symbols nobody has
tuned -- which is what the global default used to do alone.

That was an explicit requirement, and it is the kind that is easy to break
later by moving one line, so it is pinned here rather than left to reading.
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from backend.api.routes.trade import resolve_bracket_percent  # noqa: E402
from backend.core import symbol_settings  # noqa: E402
from backend.core.time_brackets import (  # noqa: E402
    parse_windows,
    resolve,
)

KOLKATA = ZoneInfo("Asia/Kolkata")
WINDOWS = parse_windows("45:20:20, *:5:10")


@pytest.fixture
def no_stored_settings(tmp_path, monkeypatch):
    """Point the per-symbol store at an empty file.

    These tests must not read the developer's real symbol_settings.json --
    NVDA is tuned in it, which would make the "nothing stored" cases pass
    or fail depending on whose machine they run on.
    """
    from backend.core import paths

    monkeypatch.setattr(paths, "STATE_DIRECTORY", tmp_path)
    (tmp_path / "us").mkdir()
    return tmp_path / "us" / "symbol_settings.json"


@pytest.fixture
def opening_window():
    """The active window during the opening stretch: 20/20."""
    from datetime import datetime

    return resolve(
        datetime(2026, 9, 23, 19, 30, tzinfo=KOLKATA),
        windows=WINDOWS,
        start=None,
        display_timezone=KOLKATA,
    )


def take_profit(**kwargs):
    """Resolve a take profit, filling in the parts these tests do not vary."""
    kwargs.setdefault("supplied", None)
    kwargs.setdefault("symbol", "NVDA")
    kwargs.setdefault("per_symbol", {})
    kwargs.setdefault("default", 7.0)
    kwargs.setdefault("name", "take profit")
    kwargs.setdefault("timezone_name", "Asia/Kolkata")
    return resolve_bracket_percent(**kwargs)


# ---------------------------------------------------------------------------
# The requirement
# ---------------------------------------------------------------------------


def test_a_symbol_tuned_in_the_page_keeps_its_bracket_in_the_window(
    no_stored_settings, opening_window
):
    """NVDA set to 5 in the page stays 5, even during the 20/20 window.

    This is the guarantee. If this test fails the feature has started
    overriding values somebody chose by hand.
    """
    symbol_settings.save("NVDA", enabled=True, take_profit=5, stop_loss=10)

    value, source = take_profit(active_window=opening_window)

    assert value == 5
    assert "set for NVDA in the UI" in source


def test_a_symbol_tuned_in_env_keeps_its_bracket_in_the_window(
    no_stored_settings, opening_window
):
    """The same holds for a symbol tuned in .env rather than the page."""
    value, source = take_profit(
        per_symbol={"NVDA": 6.0}, active_window=opening_window
    )

    assert value == 6.0
    assert "NVDA_TAKE_PROFIT_PERCENT" in source


def test_an_untuned_symbol_takes_the_window(no_stored_settings, opening_window):
    """A symbol nobody has tuned is what the clock is for."""
    value, source = take_profit(active_window=opening_window)

    assert value == 20
    assert "time window 1 of 2" in source
    assert "Asia/Kolkata" in source


def test_the_request_still_outranks_everything(no_stored_settings, opening_window):
    """A value typed for one trade wins, as it always did."""
    value, source = take_profit(supplied=12.5, active_window=opening_window)

    assert value == 12.5
    assert "supplied on the request" in source


# ---------------------------------------------------------------------------
# Off, and outside the session
# ---------------------------------------------------------------------------


def test_with_no_window_the_chain_is_exactly_as_it_was(no_stored_settings):
    """The off case: no window, so the default applies as before."""
    value, source = take_profit(active_window=None)

    assert value == 7.0
    assert source == "7% take profit, the configured default"


def test_outside_the_session_the_default_applies(no_stored_settings):
    """Before the open there is no window, so nothing changes.

    resolve() returns None outside the session, and None must mean "as
    before" rather than "window one".
    """
    from datetime import datetime

    outside = resolve(
        datetime(2026, 9, 23, 12, 0, tzinfo=KOLKATA),
        windows=WINDOWS,
        start=None,
        display_timezone=KOLKATA,
    )
    assert outside is None

    value, _ = take_profit(active_window=outside)
    assert value == 7.0


# ---------------------------------------------------------------------------
# Both legs, one clock reading
# ---------------------------------------------------------------------------


def test_both_legs_come_from_the_same_window(no_stored_settings, opening_window):
    """An order on a boundary must not straddle two windows.

    prepare_trade reads the clock once and passes the same window to both
    legs; this checks the window really does supply both.
    """
    tp, tp_source = resolve_bracket_percent(
        supplied=None,
        symbol="AAPL",
        per_symbol={},
        default=7.0,
        name="take profit",
        active_window=opening_window,
        timezone_name="Asia/Kolkata",
    )
    sl, sl_source = resolve_bracket_percent(
        supplied=None,
        symbol="AAPL",
        per_symbol={},
        default=3.0,
        name="stop loss",
        active_window=opening_window,
        timezone_name="Asia/Kolkata",
    )

    assert (tp, sl) == (20, 20)
    assert "window 1 of 2" in tp_source
    assert "window 1 of 2" in sl_source
