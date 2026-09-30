"""The Analysis page: results, the heat map, and the three kinds of stop loss."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.services import analysis

NY = ZoneInfo("America/New_York")
OPEN = time(9, 30)
T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)   # 10:00 in New York


def row(order_id, outcome, *, fill=1.00, exit_price=None, pnl=None, qty=1,
        placed=T0, held=60.0, symbol="QQQ"):
    exited = placed + timedelta(seconds=held) if exit_price is not None else None
    return {
        "order_id_text": order_id, "placed_at": placed.isoformat(),
        "filled_at": placed.isoformat(), "exited_at": exited.isoformat() if exited else None,
        "underlying": symbol, "strike": 740.0, "option_type": "CALL", "quantity": qty,
        "fill_price": fill, "exit_price": exit_price, "outcome": outcome,
        "realised_pnl": pnl, "held_seconds": held if exit_price is not None else None,
    }


def path(*prices, step=2):
    ms = int(T0.timestamp() * 1000)
    return [(ms + i * step * 1000, p) for i, p in enumerate(prices)]


def build(rows, paths):
    return analysis.build(rows, paths, timezone=NY, opens=OPEN, fallback_multiplier=100)


class TestResults:
    """The pie: how the trades ended."""

    def test_counts_and_money(self):
        rows = [row("w", "TOOK_PROFIT", exit_price=1.1, pnl=10.0),
                row("l", "STOPPED_OUT", exit_price=0.9, pnl=-10.0),
                row("l2", "STOPPED_OUT", exit_price=0.8, pnl=-20.0),
                row("m", "CLOSED_MANUALLY", exit_price=1.05, pnl=5.0),
                row("o", "STILL_OPEN")]
        r = build(rows, {})["results"]
        assert (r["total"], r["take_profit"], r["stop_loss"], r["other"], r["still_open"]) == (5, 1, 2, 1, 1)
        assert r["pnl"] == -15.0
        assert (r["wins"], r["closed"]) == (2, 4)

    def test_orders_that_bought_nothing_are_not_trades(self):
        rows = [row("w", "TOOK_PROFIT", exit_price=1.1, pnl=10.0),
                {**row("nf", "NOT_FILLED"), "fill_price": None}]
        assert build(rows, {})["results"]["total"] == 1


class TestHeatMap:

    def test_slots_are_15_minutes_after_the_open(self):
        assert analysis.slot_of(T0, NY, OPEN) == (2, "10:00 - 10:15")

    def test_money_lands_in_its_slot_and_symbol(self):
        rows = [row("q", "TOOK_PROFIT", exit_price=1.1, pnl=10.0),
                row("n", "STOPPED_OUT", exit_price=0.9, pnl=-10.0, symbol="NVDA",
                    placed=T0 + timedelta(minutes=20))]
        h = build(rows, {})["heatmap"]
        assert h["symbols"] == ["QQQ", "NVDA"]
        assert [r["slot"] for r in h["rows"]] == ["10:00 - 10:15", "10:15 - 10:30"]
        assert h["rows"][0]["cells"][0] == {"pnl": 10.0, "trades": 1, "wins": 1}
        assert h["rows"][0]["cells"][1] is None


class TestWhyStopsFired:

    def kind(self, prices, held=20, qty=1):
        # 0.10 lost per share, 100 shares a contract.
        r = row("s", "STOPPED_OUT", exit_price=0.90, pnl=-10.0 * qty, held=held, qty=qty)
        return build([r], {"s": path(*prices)})["stop_losses"]

    def test_straight_down_is_a(self):
        s = self.kind((1.00, 0.97, 0.90))
        assert s["trades"][0]["kind"] == "A" and s["kinds"]["A"]["count"] == 1
        assert "ENTRY" in s["advice"]

    def test_up_three_percent_first_is_b_and_counts_what_was_given_back(self):
        s = self.kind((1.00, 1.05, 0.90), qty=2)
        assert s["trades"][0]["kind"] == "B"
        assert s["trades"][0]["best_percent"] == pytest.approx(5.0)
        assert s["gave_back"] == pytest.approx(30.0)     # 1.05 -> 0.90, 2 x 100
        assert "EXIT" in s["advice"]

    def test_just_under_three_percent_is_not_b(self):
        assert self.kind((1.00, 1.029, 0.90))["trades"][0]["kind"] == "A"

    def test_slow_without_a_gain_is_c(self):
        assert self.kind((1.00, 0.95, 0.90), held=600)["trades"][0]["kind"] == "C"

    def test_no_recorded_prices_is_not_guessed(self):
        s = build([row("s", "STOPPED_OUT", exit_price=0.9, pnl=-10.0)], {})["stop_losses"]
        assert s["trades"][0]["kind"] is None and s["untyped"] == 1

    def test_only_stop_losses_are_listed(self):
        rows = [row("w", "TOOK_PROFIT", exit_price=1.1, pnl=10.0),
                row("s", "STOPPED_OUT", exit_price=0.9, pnl=-10.0)]
        assert [t["order_id"] for t in build(rows, {})["stop_losses"]["trades"]] == ["s"]


@pytest.mark.parametrize("fill, exit_price, qty, pnl, fallback, expected", [
    (1.00, 1.10, 2, 20.0, 100, 100),     # a US option
    (100.0, 110.0, 2, 1300.0, 1, 65),    # two NIFTY lots of 65
    (1.00, None, 1, None, 100, 100),     # nothing to recover it from
])
def test_the_multiplier_is_recovered_from_the_rows_own_money(fill, exit_price, qty, pnl, fallback, expected):
    assert analysis.multiplier_of(
        {"fill_price": fill, "exit_price": exit_price, "quantity": qty, "realised_pnl": pnl},
        fallback) == expected


def test_the_period_is_checked(fake_env):
    from backend.api.errors import ApiError
    from backend.api.routes.analysis import read_analysis

    with pytest.raises(ApiError) as backwards:
        read_analysis(market="US", start="2026-09-29", end="2026-09-01")
    assert backwards.value.error_code == "DATE_RANGE_BACKWARDS"
    with pytest.raises(ApiError) as bad:
        read_analysis(market="US", start="29-09-2026", end=None)
    assert bad.value.error_code == "DATE_INVALID"


def test_it_is_a_page_like_the_others(asset_beside):
    """In the same menu, shown and hidden by the same showPage."""
    page = asset_beside("backend.api.routes.ui", "trade_form.html")
    assert 'id="nav-analysis" onclick="showPage(\'analysis\')"' in page
    assert 'id="page-analysis"' in page
    assert '["trade", "history", "analysis", "settings"]' in page


def test_a_chart_draws_into_its_own_panel(asset_beside):
    """Found by id, an Analysis chart was drawn into the hidden History panel
    and its own box stayed empty. Two charts on one page must not collide."""
    page = asset_beside("backend.api.routes.ui", "trade_form.html")
    assert 'id="jrnychart"' not in page and 'id="jrnyhover"' not in page
    assert '$("jrnychart")' not in page and '$("jrnyhover")' not in page
    assert "jrnyDrawChart(d, box)" in page
