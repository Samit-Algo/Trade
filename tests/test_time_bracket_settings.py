"""The schedule store: .env holds the defaults, the page overrides them.

CLEARING IS NOT SETTING. The page saving a schedule and then clearing it
must get back to .env as it is READ, including any later edit to it. Writing
a copy of .env on clear would freeze it instead, which is the bug these
tests are mostly here to prevent.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from backend.core import time_bracket_settings as store  # noqa: E402
from backend.core.time_brackets import TimeBracketError  # noqa: E402


@dataclass(frozen=True)
class FakeSettings:
    """Just the four fields the store reads from configuration."""

    time_brackets_enabled: bool = False
    time_brackets_timezone: str = "Asia/Kolkata"
    time_brackets_start: str = "MARKET_OPEN"
    time_brackets_windows: str = "45:20:20, *:5:10"


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    """Point every market's state at a temporary folder, never the real one."""
    from backend.core import paths

    monkeypatch.setattr(paths, "STATE_DIRECTORY", tmp_path)
    (tmp_path / "us").mkdir()
    return tmp_path / "us" / "time_bracket_settings.json"


# ---------------------------------------------------------------------------
# Which of the two is in force
# ---------------------------------------------------------------------------


def test_with_nothing_saved_env_is_in_force(store_path):
    schedule = store.effective(FakeSettings())

    assert schedule.source == ".env"
    assert schedule.enabled is False
    assert schedule.windows_text == "45:20:20, *:5:10"


def test_saving_overrides_env(store_path):
    store.save(
        enabled=True,
        timezone_name="Asia/Singapore",
        start_text="19:00",
        windows_text="30:15:15, *:4:8",
    )

    schedule = store.effective(FakeSettings())

    assert schedule.source == "the UI"
    assert schedule.enabled is True
    assert schedule.timezone_name == "Asia/Singapore"
    assert schedule.start_text == "19:00"
    assert schedule.windows_text == "30:15:15, *:4:8"


def test_clearing_returns_to_env_as_it_is_read(store_path):
    """Not to a copy of .env taken when the override was saved.

    The .env below is DIFFERENT from the one in force when the save
    happened, standing in for someone editing .env in between. Clearing
    must pick up the edit.
    """
    store.save(
        enabled=True,
        timezone_name="Asia/Singapore",
        start_text="19:00",
        windows_text="30:15:15, *:4:8",
    )
    store.clear()

    edited_env = FakeSettings(
        time_brackets_enabled=True, time_brackets_windows="60:25:25, *:6:12"
    )
    schedule = store.effective(edited_env)

    assert schedule.source == ".env"
    assert schedule.windows_text == "60:25:25, *:6:12"


def test_clearing_when_nothing_is_saved_is_not_an_error(store_path):
    store.clear()
    store.clear()

    assert store.effective(FakeSettings()).source == ".env"


# ---------------------------------------------------------------------------
# A convenience file must never stop a trade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contents, because",
    [
        ("not json at all", "unparseable"),
        ("[]", "not an object"),
        ('{"enabled": true}', "missing the other three fields"),
        (
            '{"enabled": true, "timezone": "Mars/Olympus", '
            '"start": "MARKET_OPEN", "windows": "45:20:20, *:5:10"}',
            "a timezone that no longer resolves",
        ),
        (
            '{"enabled": true, "timezone": "Asia/Kolkata", '
            '"start": "MARKET_OPEN", "windows": "45:20:20"}',
            "a window table that no longer parses",
        ),
    ],
)
def test_an_unusable_file_falls_back_rather_than_raising(
    store_path, contents, because
):
    """Refusing to trade because a convenience file is broken is worse.

    The same reasoning symbol_settings.py uses for its own store.
    """
    store_path.write_text(contents, encoding="utf-8")

    schedule = store.effective(FakeSettings())

    assert schedule.source == ".env", because


# ---------------------------------------------------------------------------
# Validated on the way in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"timezone_name": "IST"},
        {"start_text": "7pm"},
        {"windows_text": "45:20:20"},
        {"windows_text": "45:20:150, *:5:10"},
    ],
)
def test_a_bad_schedule_is_refused_and_nothing_is_written(store_path, patch):
    good = {
        "enabled": True,
        "timezone_name": "Asia/Kolkata",
        "start_text": "MARKET_OPEN",
        "windows_text": "45:20:20, *:5:10",
    }

    with pytest.raises(TimeBracketError):
        store.save(**{**good, **patch})

    assert not store_path.exists(), "a refused save must leave no file"


def test_what_is_stored_is_what_the_parsers_read_back(store_path):
    """Normalised on the way in, so spacing typed in the page does not stick."""
    store.save(
        enabled=True,
        timezone_name="  Asia/Kolkata  ",
        start_text="MARKET_OPEN",
        windows_text="45 : 20 : 20 ,   *:5:10",
    )

    stored = json.loads(store_path.read_text(encoding="utf-8"))

    assert stored["timezone"] == "Asia/Kolkata"
    assert stored["windows"] == "45:20:20, *:5:10"
