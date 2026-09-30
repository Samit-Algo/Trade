"""India through OpenAlgo: contracts, books, the guards, and the exits.

The exits are the part that has gone wrong before, so most of this file plays
them through against a fake OpenAlgo whose books behave like the real ones.
One rule is checked throughout: there are never two exit SELLs working at the
same time. Two can both fill, and that is a short option position.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

SYMBOL = "NIFTY30SEP2625200CE"   # NIFTY at 25,118, one round strike out


# ---------------------------------------------------------------------------
# A fake OpenAlgo
# ---------------------------------------------------------------------------


class FakeOpenAlgo:
    """Just enough of OpenAlgo's client, with books that behave like its books."""

    def __init__(self):
        self.analyze = True
        self.rows: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.held: dict[str, int] = {}
        self.ltp: dict[str, float] = {SYMBOL: 100.0}
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.hide_positions = False
        self.volume: dict[str, int] = {}
        self._next = 25092900000000

    # -- orders
    def placeorder(self, **order):
        self._next += 1
        order_id = str(self._next)
        pending = order["price_type"] in ("SL", "SL-M")
        self.rows[order_id] = {
            "orderid": order_id, "symbol": order["symbol"], "exchange": order["exchange"],
            "action": order["action"], "quantity": order["quantity"],
            "price": order.get("price", 0), "trigger_price": order.get("trigger_price", 0),
            "pricetype": order["price_type"], "product": order["product"],
            "order_status": "trigger pending" if pending else "open",
            "timestamp": "2026-09-29 10:00:00",
        }
        self.placed.append({**order, "id": order_id})
        return {"status": "success", "orderid": order_id, "mode": "analyze"}

    def cancelorder(self, order_id, strategy=None):
        row = self.rows[str(order_id)]
        if row["order_status"] in ("open", "trigger pending"):
            row["order_status"] = "cancelled"
        self.cancelled.append(str(order_id))
        return {"status": "success", "orderid": order_id}

    def fill(self, order_id, price, units=None):
        row = self.rows[order_id]
        units = units or row["quantity"]
        row["order_status"] = "complete"
        self.trades.append({"orderid": order_id, "symbol": row["symbol"], "quantity": units,
                            "average_price": price, "action": row["action"],
                            "timestamp": "2026-09-29 10:00:05"})
        sign = 1 if row["action"] == "BUY" else -1
        self.held[row["symbol"]] = self.held.get(row["symbol"], 0) + sign * units

    def trigger(self, order_id):
        """A stop that fired: its limit is now resting in the ordinary book."""
        self.rows[order_id]["order_status"] = "open"

    def working_sells(self):
        return [r for r in self.rows.values()
                if r["action"] == "SELL" and r["order_status"] in ("open", "trigger pending")]

    # -- books
    def orderbook(self):
        return {"status": "success", "data": {"orders": list(self.rows.values())}}

    def tradebook(self):
        return {"status": "success", "data": list(self.trades)}

    def positionbook(self):
        rows = [] if self.hide_positions else [
            {"symbol": s, "exchange": "NFO", "product": "NRML", "quantity": q,
             "average_price": 100.0, "ltp": self.ltp.get(s, 0), "pnl": 0.0}
            for s, q in self.held.items() if q
        ]
        return {"status": "success", "data": rows}

    # -- data
    def analyzerstatus(self):
        return {"status": "success", "data": {"analyze_mode": self.analyze, "mode": "analyze"}}

    def quotes(self, symbol, exchange):
        # The index is quoted at its level; any option at its premium.
        price = self.ltp.get(symbol, 25118.0 if symbol == "NIFTY" else self.ltp[SYMBOL])
        return {"status": "success", "data": {"ltp": price, "volume": self.volume.get(symbol, 1000)}}

    def symbol(self, symbol, exchange):
        from backend.markets.india.contract import parse_identifier

        strike = parse_identifier(symbol)[3]
        return {"status": "success", "data": {
            "symbol": symbol, "name": "NIFTY", "exchange": exchange, "token": "54321",
            "lotsize": 65, "tick_size": 0.05, "strike": strike, "instrumenttype": "CE"}}

    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": ["30-SEP-26", "07-OCT-26", "14-OCT-26"]}


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Every market's state, and the audit log, in a temporary folder."""
    from backend.core import paths, safety

    monkeypatch.setattr(paths, "STATE_DIRECTORY", tmp_path / "state")
    monkeypatch.setattr(safety, "ORDER_LOG_PATH", tmp_path / "audit.log")
    return tmp_path


def india_settings(**changes):
    base = dict(market_id="IN", exchange="NFO", product="NRML", allow_live=False,
                dry_run=False, option_tick_size=0.05, lot_size=65)
    return SimpleNamespace(**{**base, **changes})


@pytest.fixture
def desk(state):
    """An exit manager over a fake OpenAlgo, with a clock that only moves when told."""
    from backend.markets.india.exits import ExitManager

    client = FakeOpenAlgo()
    market = SimpleNamespace(client=client, settings=india_settings())
    clock = Clock()
    manager = ExitManager(market, poll_seconds=1, stuck_seconds=10, slippage_ticks=20, clock=clock)
    return SimpleNamespace(client=client, market=market, clock=clock, manager=manager)


def enter(desk, *, take_profit=115.0, stop_loss=85.0, units=65):
    """Place an entry the way the market does, and start watching it."""
    order_id = desk.client.placeorder(
        strategy="t", symbol=SYMBOL, exchange="NFO", action="BUY",
        price_type="LIMIT", product="NRML", quantity=units, price=100.1)["orderid"]
    desk.manager.watch_entry(
        order_id=order_id, symbol=SYMBOL, units=units, lot_size=65, limit_price=100.1,
        take_profit=take_profit, stop_loss=stop_loss, tick=0.05, mode="PAPER")
    return order_id


def tick(desk, seconds=1):
    desk.clock.advance(seconds)
    desk.manager.tick()
    assert len(desk.client.working_sells()) <= 1, "TWO EXIT SELLS WORKING AT ONCE"


def trade(desk, entry_id):
    return next(t for t in desk.manager.trades() if t["entry_order_id"] == entry_id)


# ---------------------------------------------------------------------------
# The exits
# ---------------------------------------------------------------------------


class TestTheStopGoesOnWhenTheEntryFills:

    def test_nothing_is_placed_while_the_entry_is_waiting(self, desk):
        entry = enter(desk)
        tick(desk)
        assert len(desk.client.placed) == 1
        assert trade(desk, entry)["status"] == "WAITING_FOR_FILL"

    def test_a_filled_entry_gets_a_stop_limit_at_the_exchange(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)

        stop = desk.client.placed[-1]
        assert stop["action"] == "SELL"
        assert stop["price_type"] == "SL"
        assert stop["trigger_price"] == 85.0
        # 20 ticks below the trigger, so a moving market still fills it.
        assert stop["price"] == pytest.approx(84.0)
        assert stop["quantity"] == 65
        assert trade(desk, entry)["status"] == "PROTECTED"

    def test_the_stop_covers_only_what_filled(self, desk):
        entry = enter(desk, units=130)
        desk.client.fill(entry, 100.1, units=65)
        desk.client.rows[entry]["order_status"] = "cancelled"
        tick(desk)
        assert desk.client.placed[-1]["quantity"] == 65

    def test_an_entry_that_never_filled_places_nothing(self, desk):
        entry = enter(desk)
        desk.client.rows[entry]["order_status"] = "rejected"
        tick(desk)
        assert len(desk.client.placed) == 1
        assert trade(desk, entry)["status"] == "DONE"

    def test_a_price_already_through_the_stop_is_sold_at_once(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        desk.client.ltp[SYMBOL] = 80.0
        tick(desk)

        sell = desk.client.placed[-1]
        assert sell["price_type"] == "LIMIT"
        assert sell["price"] <= 80.0
        assert trade(desk, entry)["exit_kind"] == "STOP_LOSS"


class TestTakeProfit:

    def protected(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        return entry, desk.client.placed[-1]["id"]

    def test_the_stop_is_cancelled_before_anything_is_sold(self, desk):
        entry, stop_id = self.protected(desk)
        desk.client.ltp[SYMBOL] = 116.0
        placed_before = len(desk.client.placed)

        tick(desk)

        assert stop_id in desk.client.cancelled
        assert len(desk.client.placed) == placed_before, "sold before the stop was gone"
        assert trade(desk, entry)["status"] == "TAKING_PROFIT"

    def test_once_the_stop_is_gone_the_position_is_sold(self, desk):
        entry, stop_id = self.protected(desk)
        desk.client.ltp[SYMBOL] = 116.0
        tick(desk)   # cancel the stop
        tick(desk)   # confirmed gone: sell

        sell = desk.client.placed[-1]
        assert sell["action"] == "SELL" and sell["price_type"] == "LIMIT"
        assert sell["quantity"] == 65
        desk.client.fill(sell["id"], 115.9)
        tick(desk)

        assert trade(desk, entry)["status"] == "DONE"
        assert trade(desk, entry)["outcome"] == "TAKE_PROFIT"
        assert desk.client.held[SYMBOL] == 0

    def test_a_stop_that_fills_during_the_cancel_ends_it_with_no_second_sell(self, desk):
        """The race the old design lost: both exits filling."""
        entry, stop_id = self.protected(desk)
        desk.client.ltp[SYMBOL] = 116.0
        tick(desk)                       # cancel requested ...
        desk.client.rows[stop_id]["order_status"] = "trigger pending"
        desk.client.fill(stop_id, 84.5)  # ... but the stop filled first
        placed_before = len(desk.client.placed)

        tick(desk)

        assert len(desk.client.placed) == placed_before
        assert trade(desk, entry)["outcome"] == "STOPPED_OUT"
        assert desk.client.held[SYMBOL] == 0

    def test_a_cancel_that_does_not_land_is_asked_again(self, desk):
        entry, stop_id = self.protected(desk)
        desk.client.ltp[SYMBOL] = 116.0
        desk.client.cancelorder = lambda order_id, strategy=None: {"status": "success"}
        tick(desk)
        asked = []
        desk.client.cancelorder = lambda order_id, strategy=None: asked.append(order_id) or {"status": "success"}
        tick(desk, seconds=11)
        assert asked == [stop_id]


class TestStopLoss:

    def test_a_filled_stop_ends_the_trade(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]
        desk.client.fill(stop_id, 84.4)

        tick(desk)

        assert trade(desk, entry)["outcome"] == "STOPPED_OUT"
        assert len(desk.client.placed) == 2

    def test_a_stop_stuck_after_a_gap_goes_to_market(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]
        desk.client.trigger(stop_id)           # fired; its limit is not filling

        tick(desk)                             # first seen stuck
        tick(desk, seconds=11)                 # stuck too long: cancel it
        assert stop_id in desk.client.cancelled
        tick(desk)                             # cancelled: sell at market

        last = desk.client.placed[-1]
        assert last["price_type"] == "MARKET"
        assert last["quantity"] == 65

    def test_a_rejected_stop_is_raised_as_needing_attention(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        desk.client.rows[desk.client.placed[-1]["id"]]["order_status"] = "rejected"
        tick(desk)
        assert trade(desk, entry)["status"] == "NEEDS_ATTENTION"
        assert "NO STOP" in trade(desk, entry)["problem"]


class TestClosedByAnotherRoute:

    def test_a_released_trade_is_never_sold_again(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]

        working = desk.manager.release(SYMBOL)

        assert working == [stop_id]
        desk.client.ltp[SYMBOL] = 130.0
        placed_before = len(desk.client.placed)
        tick(desk)
        assert len(desk.client.placed) == placed_before
        assert trade(desk, entry)["status"] == "RELEASED"

    def test_a_position_closed_in_the_app_loses_its_stop(self, desk):
        """A stop left on a position no longer held would SELL SHORT."""
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]
        desk.client.held[SYMBOL] = 0          # sold from the Angel app

        for _ in range(3):
            tick(desk, seconds=6)

        assert stop_id in desk.client.cancelled
        assert trade(desk, entry)["status"] == "RELEASED"

    def test_a_lagging_position_book_does_not_cancel_the_stop(self, desk):
        """The position book can trail the order book by a moment after a fill."""
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        desk.client.hide_positions = True
        tick(desk)
        tick(desk)
        tick(desk)

        assert desk.client.cancelled == []
        assert trade(desk, entry)["status"] == "PROTECTED"


class TestTheModeCannotChangeUnderAPosition:

    def test_an_exit_is_refused_if_analyze_was_switched_off(self, desk):
        """A simulated position sold on the real account would open a real short."""
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        desk.client.analyze = False
        tick(desk)

        assert len(desk.client.placed) == 1
        assert trade(desk, entry)["status"] == "NEEDS_ATTENTION"


class TestItSurvivesARestart:

    def test_a_new_manager_carries_on_from_the_file(self, desk):
        from backend.markets.india.exits import ExitManager

        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]

        restarted = ExitManager(desk.market, poll_seconds=1, stuck_seconds=10,
                                slippage_ticks=20, clock=desk.clock)
        desk.manager = restarted
        desk.client.fill(stop_id, 84.4)
        tick(desk)

        assert trade(desk, entry)["outcome"] == "STOPPED_OUT"

    def test_the_history_knows_which_exit_belongs_to_which_entry(self, desk):
        entry = enter(desk)
        desk.client.fill(entry, 100.1)
        tick(desk)
        stop_id = desk.client.placed[-1]["id"]

        links = desk.manager.links()
        assert links[stop_id]["parent_id"] == entry
        assert links[stop_id]["role"] == "STOP_LOSS"


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


class TestTheGuards:

    def test_a_buy_is_refused_while_safe(self, state, monkeypatch):
        from backend.core import armed
        from backend.core.safety import LiveTradingBlocked
        from backend.markets.india.submit import place_entry

        client = FakeOpenAlgo()
        armed.save(dry_run=True, market_id="IN")
        with pytest.raises(LiveTradingBlocked):
            place_entry(client, india_settings(), symbol=SYMBOL, units=65, limit_price=100.0)
        assert client.placed == []

    def test_a_buy_is_refused_when_analyze_is_off_without_allow_live(self, state):
        from backend.core.safety import LiveTradingBlocked
        from backend.markets.india.submit import place_entry

        client = FakeOpenAlgo()
        client.analyze = False
        with pytest.raises(LiveTradingBlocked, match="REAL"):
            place_entry(client, india_settings(), symbol=SYMBOL, units=65, limit_price=100.0)
        assert client.placed == []

    def test_an_unreadable_mode_is_refused_not_guessed(self, state):
        from backend.core.safety import LiveTradingBlocked
        from backend.markets.india.submit import place_entry

        client = FakeOpenAlgo()
        client.analyzerstatus = lambda: {"status": "error", "message": "down"}
        with pytest.raises(LiveTradingBlocked):
            place_entry(client, india_settings(), symbol=SYMBOL, units=65, limit_price=100.0)
        assert client.placed == []

    def test_an_exit_is_never_blocked_by_the_safe_switch(self, state):
        """SAFE means no NEW trades -- never trapped in one."""
        from backend.core import armed
        from backend.markets.india.submit import place_exit

        client = FakeOpenAlgo()
        armed.save(dry_run=True, market_id="IN")
        place_exit(client, india_settings(), symbol=SYMBOL, units=65, opened_in="PAPER",
                   price_type="LIMIT", price=99.0)
        assert len(client.placed) == 1

    def test_every_india_placeorder_is_in_submit_py_behind_a_guard(self):
        from pathlib import Path

        india = Path(__file__).resolve().parent.parent / "backend" / "markets" / "india"
        callers = sorted(p.name for p in india.glob("*.py")
                         if "placeorder(" in p.read_text(encoding="utf-8"))
        assert callers == ["submit.py"]

        lines = (india / "submit.py").read_text(encoding="utf-8").splitlines()
        sends = [i for i, line in enumerate(lines) if "client.placeorder(" in line]
        assert len(sends) == 2
        for index in sends:
            before = "\n".join(lines[max(0, index - 6):index])
            assert "resolve_mode(" in before or "assert_exit_allowed(" in before


# ---------------------------------------------------------------------------
# Contracts and books
# ---------------------------------------------------------------------------


class TestContracts:

    def test_a_name_reads_both_ways(self):
        from backend.markets.india.contract import build_identifier, parse_identifier

        assert build_identifier("NIFTY", date(2026, 9, 30), 25150, "CALL") == "NIFTY30SEP2625150CE"
        assert parse_identifier(SYMBOL) == ("NIFTY", "2026-09-30", "CALL", 25200.0)
        assert parse_identifier("NIFTY30SEP2625100PE")[2] == "PUT"

    def test_a_stock_name_is_not_an_option(self):
        from backend.markets.india.contract import parse_identifier

        with pytest.raises(ValueError):
            parse_identifier("RELIANCE")

    @pytest.mark.parametrize("price, side, out, expected", [
        (25118, "CALL", 1, 25150), (25150, "CALL", 1, 25200), (25118, "CALL", 2, 25200),
        (25118, "PUT", 1, 25100), (25100, "PUT", 1, 25050), (25118, "PUT", 2, 25050),
    ])
    def test_strikes_are_out_of_the_money_like_the_us(self, price, side, out, expected):
        from backend.markets.india.contract import otm_strike

        assert otm_strike(price, side, 50, out) == expected

    def test_selection_reads_the_lot_and_tick_from_the_master(self):
        from backend.markets.india.contract import select_contract

        contract, expiry_reason, strike_reason = select_contract(
            FakeOpenAlgo(), "NIFTY", "CALL", 25118.0, exchange="NFO",
            today=date(2026, 9, 27), minimum_days=2, expiry_date_text=None, strikes_out=1,
            strike_step=100)

        assert contract.identifier == SYMBOL
        assert contract.multiplier == 65
        assert contract.min_tick == 0.05
        assert "2026-09-30" in expiry_reason

    @pytest.mark.parametrize("price, side, out, expected", [
        (22716, "CALL", 1, 22800), (22716, "PUT", 1, 22700),
        (22716, "CALL", 2, 22900), (22716, "PUT", 2, 22600),
        (22700, "CALL", 1, 22800), (22700, "PUT", 1, 22600),
    ])
    def test_round_strikes_only_with_a_step_of_100(self, price, side, out, expected):
        """Never 22,750 or 22,650 -- asked for, like the US whole strike."""
        from backend.markets.india.contract import otm_strike

        assert otm_strike(price, side, 100, out) == expected

    def test_a_step_off_the_listed_grid_refuses_to_start(self, tmp_path):
        from pathlib import Path

        from backend.core.config import ConfigError
        from backend.markets.india.config import load_india_settings

        example = Path(__file__).resolve().parent.parent / "config" / "india.env.example"
        env = tmp_path / "india.env"
        env.write_text(example.read_text(encoding="utf-8").replace(
            "STRIKE_STEP=100", "STRIKE_STEP=75"), encoding="utf-8")
        with pytest.raises(ConfigError, match="STRIKE_STEP"):
            load_india_settings(env)

    def test_the_example_asks_for_round_strikes(self, tmp_path):
        from pathlib import Path

        from backend.markets.india.config import load_india_settings

        example = Path(__file__).resolve().parent.parent / "config" / "india.env.example"
        assert load_india_settings(example).strike_step == 100

    def pick(self, volumes, side="CALL", choices=3):
        from backend.markets.india.contract import select_contract

        client = FakeOpenAlgo()
        client.volume = volumes
        contract, _, reason = select_contract(
            client, "NIFTY", side, 22716.0, exchange="NFO", today=date(2026, 9, 27),
            minimum_days=2, expiry_date_text=None, strikes_out=1,
            strike_step=100, strike_choices=choices)
        return contract.identifier, reason

    def test_the_busiest_round_strike_is_traded(self):
        name, reason = self.pick({"NIFTY30SEP2622800CE": 5000,
                                  "NIFTY30SEP2622900CE": 12000,
                                  "NIFTY30SEP2623000CE": 800})
        assert name == "NIFTY30SEP2622900CE"
        assert "busiest of 3" in reason and "22900: 12,000" in reason

    def test_only_round_strikes_are_compared(self):
        """22,750 and 22,850 are never in the running, however busy."""
        name, _ = self.pick({"NIFTY30SEP2622750CE": 99999, "NIFTY30SEP2622850CE": 99999,
                             "NIFTY30SEP2622800CE": 10, "NIFTY30SEP2622900CE": 20,
                             "NIFTY30SEP2623000CE": 30})
        assert name == "NIFTY30SEP2623000CE"

    def test_puts_compare_the_strikes_below(self):
        name, _ = self.pick({"NIFTY30SEP2622700PE": 100, "NIFTY30SEP2622600PE": 900,
                             "NIFTY30SEP2622500PE": 300}, side="PUT")
        assert name == "NIFTY30SEP2622600PE"

    def test_a_tie_goes_to_the_nearer_strike(self):
        name, _ = self.pick({"NIFTY30SEP2622800CE": 500, "NIFTY30SEP2622900CE": 500,
                             "NIFTY30SEP2623000CE": 500})
        assert name == "NIFTY30SEP2622800CE"

    def test_no_volume_takes_the_nearest_and_says_so(self):
        name, reason = self.pick({"NIFTY30SEP2622800CE": 0, "NIFTY30SEP2622900CE": 0,
                                  "NIFTY30SEP2623000CE": 0})
        assert name == "NIFTY30SEP2622800CE"
        assert "nearest was taken" in reason

    def test_one_choice_compares_nothing(self):
        name, reason = self.pick({"NIFTY30SEP2622900CE": 99999}, choices=1)
        assert name == "NIFTY30SEP2622800CE" and "volume" not in reason

    def test_an_expiry_too_close_is_skipped(self):
        from backend.markets.india.contract import select_contract

        contract, _, _ = select_contract(
            FakeOpenAlgo(), "NIFTY", "CALL", 25118.0, exchange="NFO",
            today=date(2026, 9, 29), minimum_days=2, expiry_date_text=None, strikes_out=1)
        assert contract.expiry_date_text == "2026-10-07"


class TestBooks:

    def test_quantities_are_lots_and_fills_come_from_the_trade_book(self):
        from backend.markets.india.books import fills_by_order, to_broker_order

        client = FakeOpenAlgo()
        order_id = client.placeorder(strategy="t", symbol=SYMBOL, exchange="NFO", action="BUY",
                                     price_type="LIMIT", product="NRML", quantity=130,
                                     price=100.1)["orderid"]
        client.fill(order_id, 100.05)
        # The live book shows the average once complete -- not the limit.
        client.rows[order_id]["price"] = 100.05

        order = to_broker_order(client.rows[order_id], fills_by_order(client.trades), 65,
                                {order_id: {"limit_price": 100.1}})

        assert order.quantity == 2 and order.filled == 2
        assert order.multiplier == 65
        assert order.avg_fill_price == 100.05
        assert order.limit_price == 100.1
        assert order.status == "FILLED"
        assert order.underlying == "NIFTY"


# ---------------------------------------------------------------------------
# A whole trade, through the same route the page and TradingView call
# ---------------------------------------------------------------------------


class TestAnIndiaTradeThroughTheRoute:

    @pytest.fixture
    def india(self, fake_env, state, monkeypatch):
        from pathlib import Path

        from backend.api.shared import get_market
        from backend.markets.india import data as india_data

        example = Path(__file__).resolve().parent.parent / "config" / "india.env.example"
        (fake_env / "config").mkdir()
        (fake_env / "config" / "india.env").write_text(
            example.read_text(encoding="utf-8").replace(
                "OPENALGO_API_KEY=", "OPENALGO_API_KEY=test-key"),
            encoding="utf-8")
        # Quotes carry no time; a test must not depend on the hour it runs.
        monkeypatch.setattr(india_data, "session_is_open", lambda session, moment=None: True)

        market = get_market("IN")
        market._client = FakeOpenAlgo()
        # Three days before the 30 Sep expiry, whatever day the test runs.
        monkeypatch.setattr(market, "_today", lambda: date(2026, 9, 27))
        return market

    def send(self, market, client_order_id, **fields):
        from backend.api.routes import trade
        from backend.api.schemas import TradeRequest

        body = TradeRequest(client_order_id=client_order_id, symbol="NIFTY",
                            option_type="CALL", current_price=25118.0, market="IN", **fields)
        return trade.place_bracketed_trade(body, SimpleNamespace(client=None))

    def test_safe_prices_everything_and_sends_nothing(self, india):
        response = self.send(india, "india-dry-0001")

        assert response.dry_run is True
        assert response.order_id is None
        assert response.contract.identifier == SYMBOL
        assert response.quantity == 1                       # premium 100: top band, 1 lot
        assert response.cash_required == pytest.approx(100.10 * 65)
        assert india.client.placed == []

    def test_a_cheap_premium_buys_more_lots(self, india):
        india.client.ltp[SYMBOL] = 40.0
        response = self.send(india, "india-dry-0002")
        assert response.quantity == 3                       # under 50: 3 lots
        assert response.cash_required <= 10000

    def test_over_the_cap_is_refused_in_rupees(self, india):
        from backend.api.errors import ApiError

        india.client.ltp[SYMBOL] = 200.0                    # 1 lot = 13,000
        with pytest.raises(ApiError) as refused:
            self.send(india, "india-dry-0003")
        assert refused.value.error_code == "TRADE_TOO_EXPENSIVE"
        assert "₹" in refused.value.message

    def test_armed_sends_the_buy_and_watches_its_exits(self, india):
        from backend.core import armed

        armed.save(dry_run=False, market_id="IN")
        response = self.send(india, "india-live-0001")

        buy = india.client.placed[0]
        assert buy["action"] == "BUY" and buy["price_type"] == "LIMIT"
        assert buy["quantity"] == 65                        # 1 lot, sent as units
        assert buy["exchange"] == "NFO" and buy["product"] == "NRML"
        assert str(response.order_id) == buy["id"]

        watched = india.exits.trades()
        assert len(watched) == 1 and watched[0]["status"] == "WAITING_FOR_FILL"
        assert watched[0]["mode"] == "PAPER"
        assert watched[0]["stop_loss"] < 100 < watched[0]["take_profit"]

    def test_india_is_never_armed_by_arming_the_us(self, india):
        from backend.core import armed

        armed.save(dry_run=False, market_id="US")
        response = self.send(india, "india-dry-0004")
        assert response.dry_run is True
        assert india.client.placed == []
