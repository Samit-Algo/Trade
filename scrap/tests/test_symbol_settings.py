"""Per-symbol settings set from the page.

These decide whether an order is placed at all, so the interesting cases are
the ones where a setting fails to apply: a disabled symbol that still trades,
or a blank box that zeroes a bracket instead of restoring the .env value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.core import symbol_settings  # noqa: E402
from api.service.core.symbol_settings import SymbolSettingsError  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Point the store at a temporary file, never the real one."""
    path = tmp_path / "symbol_settings.json"
    monkeypatch.setattr(symbol_settings, "SETTINGS_PATH", path)
    return path


class TestDefaults:
    """An empty store must change nothing."""

    def test_an_untouched_symbol_is_enabled(self):
        assert symbol_settings.is_enabled("NVDA")

    def test_an_untouched_symbol_has_no_overrides(self):
        stored = symbol_settings.read_for("NVDA")

        assert stored["take_profit"] is None
        assert stored["stop_loss"] is None

    def test_a_missing_file_reads_as_empty(self, store):
        assert not store.exists()
        assert symbol_settings.read_all() == {}

    def test_a_corrupt_file_reads_as_empty(self, store):
        """This is an overlay on .env. Refusing to start because a convenience
        file is malformed would be worse than ignoring it."""
        store.write_text("{not json", encoding="utf-8")

        assert symbol_settings.read_all() == {}
        assert symbol_settings.is_enabled("NVDA")


class TestEnableDisable:
    """Disabled means no NEW trades, for every caller."""

    def test_a_symbol_can_be_disabled(self):
        symbol_settings.save("NVDA", enabled=False, take_profit=None, stop_loss=None)

        assert not symbol_settings.is_enabled("NVDA")

    def test_disabling_one_leaves_the_others_alone(self):
        symbol_settings.save("NVDA", enabled=False, take_profit=None, stop_loss=None)

        assert symbol_settings.is_enabled("TSLA")

    def test_it_can_be_enabled_again(self):
        symbol_settings.save("NVDA", enabled=False, take_profit=None, stop_loss=None)
        symbol_settings.save("NVDA", enabled=True, take_profit=None, stop_loss=None)

        assert symbol_settings.is_enabled("NVDA")

    def test_the_symbol_is_matched_case_insensitively(self):
        symbol_settings.save("nvda", enabled=False, take_profit=None, stop_loss=None)

        assert not symbol_settings.is_enabled("NVDA")


class TestPercentages:
    """Bounded exactly as .env is, so the page cannot reach further."""

    def test_values_round_trip(self):
        symbol_settings.save("NVDA", enabled=True, take_profit=8, stop_loss=15)

        stored = symbol_settings.read_for("NVDA")
        assert stored["take_profit"] == 8.0
        assert stored["stop_loss"] == 15.0

    def test_null_clears_rather_than_zeroes(self):
        """A blank box must restore the .env value, not set a 0% bracket."""
        symbol_settings.save("NVDA", enabled=True, take_profit=8, stop_loss=15)
        symbol_settings.save("NVDA", enabled=True, take_profit=None, stop_loss=None)

        stored = symbol_settings.read_for("NVDA")
        assert stored["take_profit"] is None
        assert stored["stop_loss"] is None

    def test_an_empty_string_clears_too(self):
        """What an untouched HTML number input actually sends."""
        symbol_settings.save("NVDA", enabled=True, take_profit="", stop_loss="")

        assert symbol_settings.read_for("NVDA")["take_profit"] is None

    def test_zero_is_refused(self):
        with pytest.raises(SymbolSettingsError, match="greater than 0"):
            symbol_settings.save("NVDA", enabled=True, take_profit=0, stop_loss=None)

    def test_a_stop_loss_of_100_is_refused(self):
        """At 100% the stop sits at zero, which is not an exit."""
        with pytest.raises(SymbolSettingsError, match="greater than 0"):
            symbol_settings.save("NVDA", enabled=True, take_profit=None, stop_loss=100)

    def test_a_non_number_is_refused(self):
        with pytest.raises(SymbolSettingsError, match="must be a number"):
            symbol_settings.save("NVDA", enabled=True, take_profit="eight", stop_loss=None)

    def test_a_refused_save_does_not_disable_the_symbol(self):
        """The validation runs before anything is written, so a typo in a
        percentage must not leave the symbol in a half-saved state."""
        symbol_settings.save("NVDA", enabled=True, take_profit=8, stop_loss=15)

        with pytest.raises(SymbolSettingsError):
            symbol_settings.save("NVDA", enabled=False, take_profit=-5, stop_loss=None)

        assert symbol_settings.is_enabled("NVDA")
        assert symbol_settings.read_for("NVDA")["take_profit"] == 8.0


class TestTheFileItself:
    """It sits beside .env and is written the same way a config file should be."""

    def test_saving_one_symbol_keeps_the_others(self, store):
        symbol_settings.save("NVDA", enabled=True, take_profit=8, stop_loss=None)
        symbol_settings.save("TSLA", enabled=False, take_profit=None, stop_loss=20)

        stored = json.loads(store.read_text(encoding="utf-8"))
        assert set(stored) == {"NVDA", "TSLA"}
        assert stored["NVDA"]["take_profit"] == 8.0
        assert stored["TSLA"]["stop_loss"] == 20.0

    def test_it_is_gitignored(self):
        """It holds this machine's operating state, like .env."""
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        assert "symbol_settings.json" in ignored

    def test_no_temporary_file_is_left_behind(self, store):
        """Written to a temp file and moved, so an interrupted write cannot
        leave a half-written file that the next read would discard."""
        symbol_settings.save("NVDA", enabled=True, take_profit=8, stop_loss=None)

        leftovers = list(store.parent.glob(".symbol_settings-*"))
        assert leftovers == []


class TestClosingIsNeverBlocked:
    """Disabled means no NEW trades, not a position you cannot exit."""

    def test_the_close_route_does_not_consult_these_settings(self):
        source = (PROJECT_ROOT / "api/routes/close.py").read_text(encoding="utf-8")

        assert "is_enabled" not in source
        assert "symbol_settings" not in source
