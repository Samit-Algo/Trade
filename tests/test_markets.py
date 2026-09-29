"""More than one market behind the same endpoints.

The registry chooses a market by name; each market keeps its own settings
and its own page-saved switches. The line that matters most: arming one
market must never arm another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "india.env.example"


@pytest.fixture
def india_on(fake_env):
    """A project with India switched on, from the committed example file."""
    (fake_env / "config").mkdir()
    (fake_env / "config" / "india.env").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return fake_env


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Every market's page-saved state in a temporary folder."""
    from backend.core import paths

    monkeypatch.setattr(paths, "STATE_DIRECTORY", tmp_path / "state")
    return tmp_path / "state"


class TestTheRegistry:

    def test_us_is_always_traded(self, fake_env):
        from backend.api.shared import available_markets

        assert available_markets() == ["US"]

    def test_india_is_on_when_its_file_exists(self, india_on):
        from backend.api.shared import available_markets

        assert available_markets() == ["US", "IN"]

    def test_no_market_named_means_us(self, fake_env):
        """The TradingView userscript names no market, and means the US."""
        from backend.api.shared import get_market

        assert get_market(None).profile.id == "US"
        assert get_market("").profile.id == "US"
        assert get_market("us").profile.id == "US"

    def test_an_unknown_market_is_refused(self, fake_env):
        from backend.api.shared import get_market
        from backend.markets.base import UnknownMarket

        with pytest.raises(UnknownMarket):
            get_market("XX")

    def test_india_is_refused_while_its_file_is_absent(self, fake_env):
        """Not quietly served as the US instead."""
        from backend.api.shared import get_market
        from backend.markets.base import UnknownMarket

        with pytest.raises(UnknownMarket):
            get_market("IN")

    def test_each_market_is_built_once(self, india_on):
        from backend.api.shared import get_market

        assert get_market("IN") is get_market("in")
        assert get_market("IN") is not get_market("US")

    def test_the_error_codes_a_caller_can_branch_on(self):
        from backend.api.errors import classify_exception
        from backend.markets.base import MarketNotReady, UnknownMarket

        assert classify_exception(UnknownMarket("x")).status_code == 404
        assert classify_exception(UnknownMarket("x")).error_code == "UNKNOWN_MARKET"
        assert classify_exception(MarketNotReady("x")).status_code == 503
        assert classify_exception(MarketNotReady("x")).error_code == "MARKET_NOT_READY"


class TestIndiaBeforeItCanTrade:

    def test_it_answers_every_question_the_routes_ask(self):
        from backend.markets.base import Market
        from backend.markets.india.market import IndiaMarket

        assert sorted(IndiaMarket.__abstractmethods__) == []
        assert issubclass(IndiaMarket, Market)

    def test_every_broker_call_refuses_rather_than_pretends(self, india_on):
        """Nothing about India may place an order, or claim to have read one."""
        import inspect

        from backend.api.shared import get_market
        from backend.markets.base import Market, MarketNotReady

        market = get_market("IN")
        assert market.ready is False

        for name in sorted(Market.__abstractmethods__):
            method = getattr(market, name)
            arguments = [
                p for p in inspect.signature(method).parameters.values()
                if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            keywords = {
                p.name: None for p in inspect.signature(method).parameters.values()
                if p.kind is p.KEYWORD_ONLY and p.default is p.empty
            }
            with pytest.raises(MarketNotReady):
                method(*[None] * len(arguments), **keywords)

    def test_its_profile(self):
        from backend.markets.india.market import PROFILE

        assert PROFILE.id == "IN"
        assert PROFILE.currency == "INR"
        assert PROFILE.money(10000) == "₹10,000.00"
        assert str(PROFILE.timezone) == "Asia/Kolkata"
        assert PROFILE.session.opens.strftime("%H:%M") == "09:15"


class TestIndiaSettings:

    def load(self, tmp_path, **changes):
        """The example file, with some keys changed."""
        from backend.markets.india.config import load_india_settings

        lines = []
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0]
            if key in changes:
                line = f"{key}={changes.pop(key)}"
            lines.append(line)
        lines += [f"{key}={value}" for key, value in changes.items()]
        path = tmp_path / "india.env"
        path.write_text("\n".join(lines), encoding="utf-8")
        return load_india_settings(path)

    def test_the_example_is_what_was_asked_for(self, tmp_path):
        settings = self.load(tmp_path)

        assert settings.market_id == "IN"
        assert settings.trade_symbols == ("NIFTY",)
        assert settings.max_trade_cash == 10000
        assert settings.take_profit_percent == 15
        assert settings.stop_loss_percent == 15
        assert settings.trade_strikes_out == 1
        assert settings.quantity_tiers_enabled is True
        assert settings.quantity_tiers == ((50.0, 3), (75.0, 2))
        assert settings.option_tick_size == 0.05
        # 09:30 IST: 20/20 for 45 minutes, to 10:15; then 5/10.
        assert settings.time_brackets_enabled is True
        assert settings.time_brackets_timezone == "Asia/Kolkata"
        assert settings.time_brackets_start == "09:30"
        assert settings.time_brackets_windows == "45:20:20, *:5:10"

    def test_it_starts_safe(self, tmp_path):
        """A fresh copy of the file cannot spend money."""
        settings = self.load(tmp_path)

        assert settings.dry_run is True
        assert settings.allow_live is False
        assert settings.mode == "PAPER"

    def test_a_band_that_can_break_the_cap_refuses_to_start(self, tmp_path):
        """80 x 2 lots x 65 = 10,400 -- over the 10,000 cap."""
        from backend.core.config import ConfigError

        with pytest.raises(ConfigError, match="₹"):
            self.load(tmp_path, QUANTITY_TIERS="50:3, 80:2")

    def test_the_band_check_uses_this_files_lot_size(self, tmp_path):
        """At a 50 lot, 80 x 2 x 50 = 8,000 fits."""
        settings = self.load(tmp_path, QUANTITY_TIERS="50:3, 80:2", LOT_SIZE="50")

        assert settings.quantity_tiers == ((50.0, 3), (80.0, 2))

    def test_a_bracket_is_still_required(self, tmp_path):
        from backend.core.config import ConfigError

        with pytest.raises(ConfigError):
            self.load(tmp_path, TAKE_PROFIT_PERCENT="")

    def test_an_unknown_product_is_refused(self, tmp_path):
        from backend.core.config import ConfigError

        with pytest.raises(ConfigError, match="PRODUCT"):
            self.load(tmp_path, PRODUCT="CNC")


class TestEachMarketKeepsItsOwnSwitches:

    def test_arming_india_does_not_arm_the_us(self, state):
        from backend.core import armed

        armed.save(dry_run=False, market_id="IN")

        assert armed.read_stored("IN") is False
        assert armed.read_stored("US") is None

    def test_arming_the_us_does_not_arm_india(self, state):
        from backend.core import armed

        armed.save(dry_run=False, market_id="US")

        assert armed.read_stored("IN") is None

    def test_the_switch_is_read_for_the_settings_market(self, state):
        """is_dry takes settings, and settings say which market they are."""
        from types import SimpleNamespace

        from backend.core import armed

        armed.save(dry_run=False, market_id="US")

        assert armed.is_dry(SimpleNamespace(market_id="US", dry_run=True)) is False
        assert armed.is_dry(SimpleNamespace(market_id="IN", dry_run=True)) is True

    def test_symbols_are_kept_apart(self, state):
        from backend.core import symbol_settings

        symbol_settings.save("NIFTY", enabled=False, take_profit=None,
                             stop_loss=None, market_id="IN")

        assert symbol_settings.is_enabled("NIFTY", "IN") is False
        assert symbol_settings.read_all("US") == {}

    def test_schedules_are_kept_apart(self, state):
        from types import SimpleNamespace

        from backend.core import time_bracket_settings

        time_bracket_settings.save(
            enabled=True, timezone_name="Asia/Kolkata", start_text="09:30",
            windows_text="45:20:20, *:5:10", market_id="IN",
        )

        assert time_bracket_settings.read_stored("IN") is not None
        assert time_bracket_settings.read_stored("US") is None
        us = SimpleNamespace(
            market_id="US", time_brackets_enabled=False,
            time_brackets_timezone="Asia/Kolkata",
            time_brackets_start="MARKET_OPEN",
            time_brackets_windows="45:20:20, *:5:10",
        )
        assert time_bracket_settings.effective(us).source == ".env"

    def test_each_market_has_its_own_folder(self, state):
        from backend.core import armed

        armed.save(dry_run=False, market_id="IN")

        assert (state / "in" / "armed_settings.json").exists()
        assert not (state / "us").exists()


class TestIndiasOpeningHours:
    """MARKET_OPEN means each market's own open."""

    def test_market_open_in_india_is_0915_ist(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from backend.core.time_brackets import parse_windows, resolve
        from backend.markets.india.market import NSE_SESSION

        kolkata = ZoneInfo("Asia/Kolkata")
        windows = parse_windows("45:20:20, *:5:10")

        at_0920 = resolve(datetime(2026, 9, 29, 9, 20, tzinfo=kolkata), windows=windows,
                          start=None, display_timezone=kolkata, session=NSE_SESSION)
        at_1005 = resolve(datetime(2026, 9, 29, 10, 5, tzinfo=kolkata), windows=windows,
                          start=None, display_timezone=kolkata, session=NSE_SESSION)
        at_1540 = resolve(datetime(2026, 9, 29, 15, 40, tzinfo=kolkata), windows=windows,
                          start=None, display_timezone=kolkata, session=NSE_SESSION)

        assert at_0920.take_profit == 20
        assert at_1005.take_profit == 5      # 09:15 + 45 minutes is 10:00
        assert at_1540 is None               # after the 15:30 close

    def test_the_asked_for_schedule_starts_at_0930(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from backend.core.time_brackets import parse_start, parse_windows, resolve

        kolkata = ZoneInfo("Asia/Kolkata")
        windows = parse_windows("45:20:20, *:5:10")
        start = parse_start("09:30")

        def at(hour, minute):
            return resolve(datetime(2026, 9, 29, hour, minute, tzinfo=kolkata),
                           windows=windows, start=start, display_timezone=kolkata)

        assert at(9, 20) is None                       # before 09:30: the default
        assert (at(9, 30).take_profit, at(9, 30).stop_loss) == (20, 20)
        assert (at(10, 14).take_profit, at(10, 14).stop_loss) == (20, 20)
        assert (at(10, 15).take_profit, at(10, 15).stop_loss) == (5, 10)
        assert (at(14, 0).take_profit, at(14, 0).stop_loss) == (5, 10)


class TestAFailedCheckDoesNotStrandTheRequest:
    """A symbol check that cannot read the broker sent nothing.

    So the caller's client_order_id must be given back. Keeping it answered
    every retry with REQUEST_IN_FLIGHT for a request nothing was working on.
    """

    def test_the_id_is_released_when_the_broker_cannot_be_read(
        self, fake_env, state, monkeypatch
    ):
        from types import SimpleNamespace

        from backend.api.routes import trade
        from backend.api.schemas import TradeRequest
        from backend.api.shared import get_idempotency_store, get_market

        class Unreachable:
            ready = True
            profile = get_market("US").profile
            settings = get_market("US").settings

            def open_orders(self):
                raise ConnectionError("broker unreachable")

        monkeypatch.setattr(trade, "get_market", lambda _market=None: Unreachable())
        body = TradeRequest(
            client_order_id="retry-me-0001", symbol="TSLA", option_type="CALL"
        )

        with pytest.raises(ConnectionError):
            trade.place_bracketed_trade(body, SimpleNamespace(client=None))

        # Free to be claimed again: nothing is holding it.
        assert get_idempotency_store().claim("retry-me-0001") is None

    def test_a_market_that_cannot_trade_is_refused_before_claiming(
        self, india_on, state
    ):
        from types import SimpleNamespace

        from backend.api.routes import trade
        from backend.api.schemas import TradeRequest
        from backend.api.shared import get_idempotency_store
        from backend.markets.base import MarketNotReady

        body = TradeRequest(
            client_order_id="india-0000001", symbol="NIFTY",
            option_type="CALL", market="IN",
        )

        with pytest.raises(MarketNotReady):
            trade.place_bracketed_trade(body, SimpleNamespace(client=None))

        assert get_idempotency_store().claim("india-0000001") is None
