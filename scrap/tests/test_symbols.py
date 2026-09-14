"""TRADE_SYMBOLS, and the per-symbol bracket percentages.

The point of these is that a percentage set for one symbol reaches that symbol
and nothing else. A bracket applied to the wrong contract is real money, and
it fails silently -- the order goes through, at the wrong levels.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.routes.trade import resolve_bracket_percent  # noqa: E402
from api.service.core.config import (  # noqa: E402
    ConfigError,
    _get_symbol_list,
    _get_symbol_percentages,
)


class TestSymbolList:
    """One list, read by both the /ui dropdown and the userscript."""

    def test_a_list_is_parsed_and_upper_cased(self, monkeypatch):
        monkeypatch.setenv("TRADE_SYMBOLS", "tsla, aapl, qqq")

        assert _get_symbol_list("TRADE_SYMBOLS", ()) == ("TSLA", "AAPL", "QQQ")

    def test_order_is_preserved(self, monkeypatch):
        """The dropdown lists them as written, so the order is a choice."""
        monkeypatch.setenv("TRADE_SYMBOLS", "QQQ, TSLA, AAPL")

        assert _get_symbol_list("TRADE_SYMBOLS", ()) == ("QQQ", "TSLA", "AAPL")

    def test_duplicates_are_dropped(self, monkeypatch):
        monkeypatch.setenv("TRADE_SYMBOLS", "TSLA, tsla, QQQ")

        assert _get_symbol_list("TRADE_SYMBOLS", ()) == ("TSLA", "QQQ")

    def test_absent_takes_the_default(self, monkeypatch):
        monkeypatch.delenv("TRADE_SYMBOLS", raising=False)

        assert _get_symbol_list("TRADE_SYMBOLS", ("TSLA",)) == ("TSLA",)

    def test_set_to_nothing_is_refused(self, monkeypatch):
        """An empty list leaves nothing to trade. That is a typo, not an
        intention -- the way to take the default is to leave it out."""
        monkeypatch.setenv("TRADE_SYMBOLS", " , ,")

        with pytest.raises(ConfigError, match="no symbols"):
            _get_symbol_list("TRADE_SYMBOLS", ("TSLA",))


class TestSymbolPercentages:
    """Per-symbol overrides, read BY the symbol list."""

    SYMBOLS = ("TSLA", "AAPL", "QQQ")

    def test_only_what_is_set_appears(self, monkeypatch):
        monkeypatch.setenv("TSLA_TAKE_PROFIT_PERCENT", "12")
        monkeypatch.delenv("AAPL_TAKE_PROFIT_PERCENT", raising=False)
        monkeypatch.delenv("QQQ_TAKE_PROFIT_PERCENT", raising=False)

        found = _get_symbol_percentages(
            "_TAKE_PROFIT_PERCENT", self.SYMBOLS, maximum=1000, inclusive=True
        )

        assert found == {"TSLA": 12.0}

    def test_a_symbol_not_on_the_list_is_never_read(self, monkeypatch):
        """NVDA_TAKE_PROFIT_PERCENT with NVDA absent from TRADE_SYMBOLS is
        inert -- it sits in .env doing nothing rather than erroring."""
        monkeypatch.setenv("NVDA_TAKE_PROFIT_PERCENT", "8")

        found = _get_symbol_percentages(
            "_TAKE_PROFIT_PERCENT", self.SYMBOLS, maximum=1000, inclusive=True
        )

        assert "NVDA" not in found

    def test_the_bounds_match_the_global(self, monkeypatch):
        """A per-symbol override must not reach a value the default could
        not. Stop loss is exclusive of 100: at 100 the stop sits at zero."""
        monkeypatch.setenv("TSLA_STOP_LOSS_PERCENT", "100")

        with pytest.raises(ConfigError, match="greater than 0"):
            _get_symbol_percentages(
                "_STOP_LOSS_PERCENT", self.SYMBOLS, maximum=100, inclusive=False
            )

    def test_zero_is_refused(self, monkeypatch):
        monkeypatch.setenv("TSLA_TAKE_PROFIT_PERCENT", "0")

        with pytest.raises(ConfigError, match="greater than 0"):
            _get_symbol_percentages(
                "_TAKE_PROFIT_PERCENT", self.SYMBOLS, maximum=1000, inclusive=True
            )

    def test_a_non_number_is_refused(self, monkeypatch):
        monkeypatch.setenv("TSLA_TAKE_PROFIT_PERCENT", "twelve")

        with pytest.raises(ConfigError, match="must be a number"):
            _get_symbol_percentages(
                "_TAKE_PROFIT_PERCENT", self.SYMBOLS, maximum=1000, inclusive=True
            )


class TestResolutionOrder:
    """Request, then the symbol's own setting, then the default."""

    PER_SYMBOL = {"TSLA": 12.0}
    DEFAULT = 5.0

    def resolve(self, supplied, symbol):
        return resolve_bracket_percent(
            supplied=supplied,
            symbol=symbol,
            per_symbol=self.PER_SYMBOL,
            default=self.DEFAULT,
            name="take profit",
        )

    def test_the_symbols_own_setting_beats_the_default(self):
        value, source = self.resolve(None, "TSLA")

        assert value == 12.0
        assert "TSLA_TAKE_PROFIT_PERCENT" in source

    def test_a_symbol_without_one_takes_the_default(self):
        value, source = self.resolve(None, "AAPL")

        assert value == 5.0
        assert "default" in source

    def test_the_request_beats_the_symbols_setting(self):
        """An explicit value is for this trade only, and it wins outright."""
        value, source = self.resolve(99.0, "TSLA")

        assert value == 99.0
        assert "request" in source

    def test_the_symbol_is_matched_case_insensitively(self):
        """The underlying arrives from Tiger, whose casing is its own."""
        value, _ = self.resolve(None, "tsla")

        assert value == 12.0

    def test_surrounding_whitespace_does_not_hide_a_setting(self):
        value, _ = self.resolve(None, " TSLA ")

        assert value == 12.0

    def test_the_source_always_names_where_it_came_from(self):
        """A bracket that is not the one expected must be traceable, so no
        path returns an empty or generic reason."""
        for supplied, symbol in ((None, "TSLA"), (None, "AAPL"), (7.0, "QQQ")):
            _, source = self.resolve(supplied, symbol)
            assert source.strip()
            assert "take profit" in source


class TestNothingIsHardcoded:
    """The symbol list lives in .env, in one place.

    It used to be written in the /ui dropdown AND in the userscript, and the
    two had drifted: the page offered AAPL, the userscript did not recognise
    it, so a chart the page could trade the script would refuse.
    """

    def test_the_page_has_no_symbol_options(self):
        page = (PROJECT_ROOT / "api/routes/trade_form.html").read_text(encoding="utf-8")

        for symbol in ("AAPL", "NVDA", "TSLA", "QQQ"):
            assert f"<option>{symbol}</option>" not in page

    def test_the_userscript_has_no_symbol_array(self):
        script = (PROJECT_ROOT / "scrap/callOrderAPI.user.js").read_text(encoding="utf-8")

        assert '["TSLA", "QQQ", "NVDA"]' not in script
        assert "trade_symbols" in script
