"""Unit tests for configuration parsing. No network, no SDK, no credentials."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service.core.config import ConfigError, load_settings  # noqa: E402
from api.service.core.safety import LiveTradingBlocked  # noqa: E402

PAPER = "20191106192858300"

BASE_ENV = {
    "TIGER_ID": "123456",
    "TIGER_ACCOUNT": PAPER,
    "TIGER_PAPER_ACCOUNT": PAPER,
    "TIGER_ALLOW_LIVE": "false",
    "DRY_RUN": "true",
    # Required, and deliberately undefaulted in config.py.
    "TAKE_PROFIT_PERCENT": "20",
    "STOP_LOSS_PERCENT": "15",
}

TIGER_VARS = [
    "TIGER_ID",
    "TIGER_ACCOUNT",
    "TIGER_PAPER_ACCOUNT",
    "TIGER_PRIVATE_KEY_PATH",
    "TIGER_ALLOW_LIVE",
    "TIGER_LICENSE",
    "DRY_RUN",
    # Phase 11. Cleared between tests like everything else, so a value in the
    # developer's real environment cannot change an outcome here.
    "TRADE_QUANTITY",
    "TAKE_PROFIT_PERCENT",
    "STOP_LOSS_PERCENT",
    "TRADE_STRIKES_OUT",
    "TRADE_EXPIRY_DATE",
    "LEG_TIME_IN_FORCE",
    "REQUIRE_LIVE_TRADING",
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


# ---------------------------------------------------------------------------
# Phase 11 -- the trading decisions that moved out of the request body
#
# Every bound here used to be a Pydantic Field on TradeRequest. Moving them to
# startup is what makes POST /trade a four-input call; these tests are what
# makes sure the validation came with them.
# ---------------------------------------------------------------------------


def test_phase_11_defaults(env):
    settings = env()
    assert settings.trade_quantity == 1
    assert settings.trade_strikes_out == 1
    assert settings.trade_expiry_date is None
    assert settings.leg_time_in_force == "DAY"
    assert settings.require_live_trading is False


def test_the_bracket_percentages_are_read(env):
    settings = env(TAKE_PROFIT_PERCENT="42.5", STOP_LOSS_PERCENT="7")
    assert settings.take_profit_percent == 42.5
    assert settings.stop_loss_percent == 7


@pytest.mark.parametrize("name", ["TAKE_PROFIT_PERCENT", "STOP_LOSS_PERCENT"])
def test_a_missing_bracket_percentage_refuses_to_boot(env, name):
    """No default, on purpose: a guessed bracket is a guess about real money."""
    with pytest.raises(ConfigError, match="required"):
        env(**{name: None})


@pytest.mark.parametrize(
    "name,value",
    [
        ("TAKE_PROFIT_PERCENT", "0"),
        ("TAKE_PROFIT_PERCENT", "-5"),
        ("TAKE_PROFIT_PERCENT", "1001"),
        ("STOP_LOSS_PERCENT", "0"),
        ("STOP_LOSS_PERCENT", "-1"),
        ("STOP_LOSS_PERCENT", "100"),
        ("STOP_LOSS_PERCENT", "150"),
    ],
)
def test_out_of_range_percentages_are_rejected(env, name, value):
    with pytest.raises(ConfigError):
        env(**{name: value})


def test_a_take_profit_of_exactly_1000_is_allowed(env):
    """The old Field was le=1000, not lt."""
    assert env(TAKE_PROFIT_PERCENT="1000").take_profit_percent == 1000


@pytest.mark.parametrize("value", ["0", "-1", "1001"])
def test_out_of_range_quantity_is_rejected(env, value):
    with pytest.raises(ConfigError, match="between 1 and 1000"):
        env(TRADE_QUANTITY=value)


@pytest.mark.parametrize("value", ["0", "11"])
def test_out_of_range_strikes_out_is_rejected(env, value):
    with pytest.raises(ConfigError, match="between 1 and 10"):
        env(TRADE_STRIKES_OUT=value)


def test_an_explicit_expiry_is_kept(env):
    assert env(TRADE_EXPIRY_DATE="2026-09-18").trade_expiry_date == "2026-09-18"


@pytest.mark.parametrize("value", ["18-09-2026", "2026-13-01", "next friday"])
def test_a_malformed_expiry_is_rejected(env, value):
    """Falling back to auto-selection would trade a different contract."""
    with pytest.raises(ConfigError, match="YYYY-MM-DD"):
        env(TRADE_EXPIRY_DATE=value)


def test_leg_time_in_force_is_case_insensitive(env):
    assert env(LEG_TIME_IN_FORCE="gtc").leg_time_in_force == "GTC"


def test_an_unknown_time_in_force_is_rejected(env):
    with pytest.raises(ConfigError, match="LEG_TIME_IN_FORCE"):
        env(LEG_TIME_IN_FORCE="IOC")


def test_require_live_trading_parses_strictly(env):
    assert env(REQUIRE_LIVE_TRADING="true").require_live_trading is True
    with pytest.raises(ConfigError):
        env(REQUIRE_LIVE_TRADING="maybe")
