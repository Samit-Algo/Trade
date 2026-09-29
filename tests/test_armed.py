"""The arming switch.

This decides whether an order reaches the broker, so the tests are written
around one question: can anything here make the service armed when nobody
armed it? Every failure path is checked to fall SAFE.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from backend.core import armed  # noqa: E402
from backend.core.safety import LiveTradingBlocked, assert_order_allowed  # noqa: E402


@dataclass(frozen=True)
class FakeSettings:
    """Just the fields the switch reads."""

    dry_run: bool = True
    mode: str = "PAPER"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at a temporary file, never the real one."""
    path = tmp_path / "armed_settings.json"
    monkeypatch.setattr(armed, "SETTINGS_PATH", path)
    return path


# ---------------------------------------------------------------------------
# Which of the two is in force
# ---------------------------------------------------------------------------


def test_with_nothing_stored_env_decides(store):
    assert armed.is_dry(FakeSettings(dry_run=True)) is True
    assert armed.is_dry(FakeSettings(dry_run=False)) is False


def test_the_switch_overrides_env_both_ways(store):
    """Including arming a service whose .env says dry run.

    That is the point of the feature: .env stays safe, and the page arms it
    for the session without a restart.
    """
    armed.save(dry_run=False)
    assert armed.is_dry(FakeSettings(dry_run=True)) is False

    armed.save(dry_run=True)
    assert armed.is_dry(FakeSettings(dry_run=False)) is True


def test_clearing_returns_to_env(store):
    armed.save(dry_run=False)
    armed.clear()

    assert armed.is_dry(FakeSettings(dry_run=True)) is True
    assert armed.describe(FakeSettings())[1] == ".env"


def test_describe_names_the_source(store):
    assert armed.describe(FakeSettings(dry_run=True)) == (True, ".env")

    armed.save(dry_run=False)
    assert armed.describe(FakeSettings(dry_run=True)) == (False, "the UI")


def test_the_switch_survives_a_restart(store):
    """The file IS the persistence: a new read sees what was written."""
    armed.save(dry_run=False)

    assert armed.read_stored() is False
    assert armed.is_dry(FakeSettings(dry_run=True)) is False


# ---------------------------------------------------------------------------
# Every failure falls SAFE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contents, because",
    [
        ("not json at all", "unparseable"),
        ("[]", "not an object"),
        ("{}", "no dry_run key"),
        ('{"armed": true}', "the wrong key -- must not be read as arming"),
    ],
)
def test_an_unusable_file_falls_back_to_env(store, contents, because):
    """A corrupt file must never arm anything.

    Falling back to .env is the safe direction, because .env defaults to dry
    run. The wrong-key case matters most: the file stores `dry_run`, and a
    file holding `armed: true` must not be read as arming by accident.
    """
    store.write_text(contents, encoding="utf-8")

    assert armed.read_stored() is None
    assert armed.is_dry(FakeSettings(dry_run=True)) is True, because


def test_a_missing_file_is_not_an_error(store):
    assert armed.read_stored() is None
    assert armed.is_dry(FakeSettings(dry_run=True)) is True


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_the_guard_refuses_while_safe(store):
    """assert_order_allowed is the last thing before place_order."""
    with pytest.raises(LiveTradingBlocked):
        assert_order_allowed("PAPER", armed.is_dry(FakeSettings(dry_run=True)))


def test_the_guard_allows_once_armed(store):
    armed.save(dry_run=False)

    assert_order_allowed("PAPER", armed.is_dry(FakeSettings(dry_run=True)))


def test_arming_does_not_defeat_the_paper_lock(store):
    """The switch decides whether to send, not what to send it to.

    A non-paper account is refused whatever the switch says.
    """
    armed.save(dry_run=False)

    with pytest.raises(LiveTradingBlocked):
        assert_order_allowed("LIVE", armed.is_dry(FakeSettings(dry_run=True)))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_saving_writes_the_value_the_guards_read(store):
    import json

    armed.save(dry_run=False)

    assert json.loads(store.read_text(encoding="utf-8")) == {"dry_run": False}


def test_clearing_twice_is_not_an_error(store):
    armed.clear()
    armed.clear()

    assert armed.read_stored() is None
