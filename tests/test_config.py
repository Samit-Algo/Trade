"""Unit tests for configuration parsing. No network, no SDK, no credentials."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tiger_backend.config import ConfigError, load_settings  # noqa: E402
from tiger_backend.safety import LiveTradingBlocked  # noqa: E402

PAPER = "20191106192858300"

BASE_ENV = {
    "TIGER_ID": "123456",
    "TIGER_ACCOUNT": PAPER,
    "TIGER_PAPER_ACCOUNT": PAPER,
    "TIGER_ALLOW_LIVE": "false",
    "DRY_RUN": "true",
}

TIGER_VARS = [
    "TIGER_ID",
    "TIGER_ACCOUNT",
    "TIGER_PAPER_ACCOUNT",
    "TIGER_PRIVATE_KEY_PATH",
    "TIGER_ALLOW_LIVE",
    "TIGER_LICENSE",
    "DRY_RUN",
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A clean environment plus a stand-in key file. Never touches a real .env."""
    for name in TIGER_VARS:
        monkeypatch.delenv(name, raising=False)

    key = tmp_path / "key.pem"
    key.write_text("not-a-real-key")

    # An empty stand-in .env. Passing env_file=None would make load_settings
    # read the developer's real .env at the project root, so a filled-in
    # credential would leak into the tests and change their outcome.
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")

    def configure(**overrides):
        values = {**BASE_ENV, "TIGER_PRIVATE_KEY_PATH": str(key), **overrides}
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return load_settings(env_file=empty_env)

    configure.key_path = key
    return configure


def test_defaults_are_paper_and_dry_run(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = env(TIGER_ALLOW_LIVE=None, DRY_RUN=None)
    assert settings.mode == "PAPER"
    assert settings.dry_run is True
    assert settings.allow_live is False


def test_missing_values_are_reported_together(env):
    with pytest.raises(ConfigError) as exc:
        env(TIGER_ID="", TIGER_ACCOUNT="")
    message = str(exc.value)
    assert "TIGER_ID" in message and "TIGER_ACCOUNT" in message


def test_surrounding_whitespace_is_stripped(env):
    """A copy-pasted account ID with a trailing space must still match."""
    settings = env(TIGER_ACCOUNT=f"  {PAPER} ")
    assert settings.account == PAPER
    assert settings.mode == "PAPER"


def test_unparseable_boolean_is_rejected(env):
    with pytest.raises(ConfigError, match="true or false"):
        env(DRY_RUN="maybe")


def test_live_account_without_opt_in_is_blocked(env):
    with pytest.raises(LiveTradingBlocked):
        env(TIGER_ACCOUNT="U12300123")


def test_live_account_with_opt_in_resolves_live(env):
    settings = env(TIGER_ACCOUNT="U12300123", TIGER_ALLOW_LIVE="true")
    assert settings.mode == "LIVE"


def test_missing_private_key_is_reported(env, tmp_path):
    with pytest.raises(ConfigError, match="Private key not found"):
        env(TIGER_PRIVATE_KEY_PATH=str(tmp_path / "absent.pem"))


def test_settings_are_frozen(env):
    settings = env()
    with pytest.raises(Exception):
        settings.dry_run = False  # type: ignore[misc]


def test_masked_account_property(env):
    assert env().masked_account == "****8300"


def test_license_is_optional(env):
    assert env().license is None
    assert env(TIGER_LICENSE="TBSG").license == "TBSG"


def test_properties_file_as_key_path_is_rejected(env, tmp_path):
    """The exact mistake that produced 'Invalid symbol 95' from deep in the SDK."""
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("private_key_pk1=MIICXQIBAAKBgQ\nprivate_key_pk8=MIICdwIBADANBg\n")
    with pytest.raises(ConfigError, match="properties file"):
        env(TIGER_PRIVATE_KEY_PATH=str(props))


def test_empty_key_file_is_rejected(env, tmp_path):
    blank = tmp_path / "blank.pem"
    blank.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        env(TIGER_PRIVATE_KEY_PATH=str(blank))


def test_real_base64_key_with_padding_is_accepted(env, tmp_path):
    """Base64 padding ends with '=' -- that must not read as a properties line."""
    real = tmp_path / "real.pem"
    real.write_text("MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAI==\n")
    assert env(TIGER_PRIVATE_KEY_PATH=str(real)).private_key_path == real
