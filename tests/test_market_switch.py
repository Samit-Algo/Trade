"""Which markets run, and switching India on and off from the page.

The problem this exists for: India read OpenAlgo in the background whenever
config/india.env existed, so a US-only day -- or a US-only machine -- printed
an OpenAlgo error every few seconds. MARKETS in config/server.env decides what
runs at all; the Settings page switches India on and off without a restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "india.env.example"


@pytest.fixture
def india_file(fake_env, monkeypatch):
    """config/india.env present, from the committed example, with a key."""
    (fake_env / "config").mkdir()
    (fake_env / "config" / "india.env").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("OPENALGO_API_KEY", "test-openalgo-key")
    monkeypatch.delenv("MARKETS", raising=False)
    return fake_env


# ---------------------------------------------------------------------------
# MARKETS in config/server.env
# ---------------------------------------------------------------------------


class TestTheMarketsLine:

    def test_us_alone_never_starts_india_even_with_its_file(self, india_file, monkeypatch):
        from backend.api.shared import available_markets

        monkeypatch.setenv("MARKETS", "US")
        assert available_markets() == ["US"]

    def test_both(self, india_file, monkeypatch):
        from backend.api.shared import available_markets

        monkeypatch.setenv("MARKETS", " us , in ")
        assert available_markets() == ["US", "IN"]

    def test_us_is_always_included_and_first(self, india_file, monkeypatch):
        from backend.api.shared import available_markets

        monkeypatch.setenv("MARKETS", "IN")
        assert available_markets() == ["US", "IN"]

    def test_without_the_line_the_file_decides_as_before(self, india_file):
        from backend.api.shared import available_markets

        assert available_markets() == ["US", "IN"]

    def test_india_listed_without_its_file_is_a_clear_error(self, fake_env, monkeypatch):
        from backend.api.shared import available_markets
        from backend.core.config import ConfigError

        monkeypatch.setenv("MARKETS", "US,IN")
        with pytest.raises(ConfigError, match="india.env"):
            available_markets()

    def test_an_unknown_market_is_refused_at_startup(self, fake_env, monkeypatch):
        from backend.api.shared import get_settings
        from backend.core.config import ConfigError

        monkeypatch.setenv("MARKETS", "US,FX")
        with pytest.raises(ConfigError, match="FX"):
            get_settings()


# ---------------------------------------------------------------------------
# The page's switch
# ---------------------------------------------------------------------------


class TestTheSwitch:

    def test_on_when_nothing_is_stored(self):
        from backend.core import market_switch

        assert market_switch.is_on("IN") is True

    def test_saved_per_market(self):
        from backend.core import market_switch

        market_switch.save(False, "IN")
        assert market_switch.is_on("IN") is False
        assert market_switch.is_on("US") is True
        market_switch.save(True, "IN")
        assert market_switch.is_on("IN") is True

    def test_a_broken_file_reads_as_on(self):
        from backend.core import market_switch

        path = market_switch.settings_path("IN")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert market_switch.is_on("IN") is True


class TestIndiaSwitchedOff:

    def test_not_ready_and_says_why(self, india_file):
        from backend.api.shared import get_market
        from backend.core import market_switch
        from backend.markets.base import MarketNotReady

        india = get_market("IN")
        assert india.ready is True

        market_switch.save(False, "IN")
        assert india.ready is False          # no restart
        with pytest.raises(MarketNotReady, match="switched off"):
            india.positions()

    def test_the_route_switches_it_and_lists_it(self, india_file):
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn

        row = health.switch_market("IN", MarketSwitchIn(on=False))
        assert row.switched_on is False and row.ready is False and row.can_switch

        listed = {m.id: m for m in health.read_markets().markets}
        assert listed["IN"].switched_on is False
        assert listed["US"].switched_on is True and not listed["US"].can_switch

        assert health.switch_market("IN", MarketSwitchIn(on=True)).ready is True

    def test_the_us_cannot_be_switched_off(self, india_file):
        from backend.api.errors import ApiError
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn

        with pytest.raises(ApiError) as caught:
            health.switch_market("US", MarketSwitchIn(on=False))
        assert caught.value.status_code == 400

    def test_refused_while_a_take_profit_is_being_watched(self, india_file):
        """Switched off, nothing would watch it -- the take profit would never fire."""
        from backend.api.errors import ApiError
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn
        from backend.api.shared import get_market
        from backend.core import market_switch
        from backend.markets.india.exits import PROTECTED

        india = get_market("IN")
        india.exits.trades = lambda: [{"symbol": "NIFTY06OCT2622600PE", "status": PROTECTED}]

        with pytest.raises(ApiError) as caught:
            health.switch_market("IN", MarketSwitchIn(on=False))
        assert caught.value.status_code == 409
        assert "NIFTY06OCT2622600PE" in caught.value.message
        assert market_switch.is_on("IN") is True

    def test_switching_on_is_never_refused(self, india_file):
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn
        from backend.api.shared import get_market
        from backend.core import market_switch
        from backend.markets.india.exits import PROTECTED

        market_switch.save(False, "IN")
        get_market("IN").exits.trades = lambda: [{"symbol": "X", "status": PROTECTED}]
        assert health.switch_market("IN", MarketSwitchIn(on=True)).switched_on is True


# ---------------------------------------------------------------------------
# The price recorder: only when the market says so
# ---------------------------------------------------------------------------


class TestRecordingHours:

    def test_india_records_only_in_nse_hours(self, india_file, monkeypatch):
        from backend.api.shared import get_market
        from backend.markets.india import data

        india = get_market("IN")
        at = {"now": None}
        real = data.session_is_open
        monkeypatch.setattr(data, "session_is_open", lambda session: real(session, at["now"]))

        # Wednesday 30 Sep 2026, IST = UTC + 5:30.
        at["now"] = datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc)    # 10:30 IST
        assert india.recording_now() is True
        at["now"] = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)   # 19:30 IST
        assert india.recording_now() is False
        at["now"] = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)    # Saturday
        assert india.recording_now() is False

    def test_the_us_records_as_before(self, fake_env):
        from backend.api.shared import get_market

        assert get_market("US").recording_now() is True

    def test_an_inactive_recorder_asks_nothing(self, tmp_path):
        from backend.services.market.price_log import PriceLog, PriceRecorder

        def read(_age):
            raise AssertionError("OpenAlgo must not be asked")

        recorder = PriceRecorder(read, PriceLog(tmp_path), active=lambda: False)
        assert recorder.tick() == 0


class TestSwitchOffAnyway:
    """The page's second step, after the warning is confirmed."""

    TRADE = {"symbol": "NIFTY06OCT2622600PE", "status": "PROTECTED",
             "take_profit": 124.85, "stop_loss": 107.0}

    def test_the_warning_names_both_levels(self, india_file):
        from backend.api.shared import get_market

        india = get_market("IN")
        india.exits.trades = lambda: [dict(self.TRADE)]
        reason = india.cannot_switch_off()
        assert "take profit ₹124.85 will NOT be watched" in reason
        assert "stop loss ₹107.00" in reason

    def test_force_switches_it_off(self, india_file):
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn
        from backend.api.shared import get_market

        get_market("IN").exits.trades = lambda: [dict(self.TRADE)]
        row = health.switch_market("IN", MarketSwitchIn(on=False, force=True))
        assert row.switched_on is False

    def test_off_with_a_trade_open_is_an_alert_on_every_page(self, india_file):
        from backend.api.routes import health
        from backend.api.schemas import MarketSwitchIn
        from backend.api.shared import get_market

        get_market("IN").exits.trades = lambda: [dict(self.TRADE)]
        health.switch_market("IN", MarketSwitchIn(on=False, force=True))

        alerts = {m.id: m.alerts for m in health.read_markets().markets}["IN"]
        assert any("not being watched" in a and "₹124.85" in a for a in alerts)

    def test_the_exit_watcher_asks_nothing_while_off(self, india_file):
        from backend.api.shared import get_market
        from backend.core import market_switch
        from backend.markets.india.exits import PROTECTED

        india = get_market("IN")
        india.exits._trades = {"1": {"symbol": "X", "status": PROTECTED}}
        market_switch.save(False, "IN")

        class NoClient:
            def __getattr__(self, name):
                raise AssertionError("OpenAlgo must not be asked while India is off")

        india._client = NoClient()
        india.exits.tick()   # returns without touching the client
