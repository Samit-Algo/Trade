"""Recorded prices, and the line a trade journey is drawn as.

The bug this exists for: the chart's line was minute CLOSES, the dots were
the fill, the exit and a minute's high -- none of them a close -- so every
dot sat off the line. The line is now built to start at the fill and end at
the exit, with recorded prices in between.

No network, no broker. The recorder is fed a fake position reader.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.market.bars import Bar  # noqa: E402
from backend.services.market.price_log import PriceLog, PriceRecorder  # noqa: E402
from backend.services.order import journey  # noqa: E402

#: 2026-09-24 20:11 UTC.
START_MS = 1790280660000
CONTRACT = "TSLA  260928P00370000"
DAY = 86_400_000


def bar(minute, c, h=None, l=None):
    return Bar(time_ms=START_MS + minute * 60_000, open=c,
               high=h if h is not None else c, low=l if l is not None else c,
               close=c, volume=1)


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


def test_a_price_that_does_not_move_is_written_once(tmp_path):
    log = PriceLog(tmp_path)
    assert log.record(CONTRACT, START_MS, 3.60)
    assert not log.record(CONTRACT, START_MS + 2000, 3.60)
    assert log.record(CONTRACT, START_MS + 4000, 3.62)

    assert log.read(CONTRACT, START_MS, START_MS + 10_000) == [
        (START_MS, 3.60), (START_MS + 4000, 3.62),
    ]


def test_the_folder_name_has_no_spaces(tmp_path):
    PriceLog(tmp_path).record(CONTRACT, START_MS, 1.0)
    assert (tmp_path / "TSLA260928P00370000").is_dir()


def test_reading_is_limited_to_the_trade(tmp_path):
    """Two trades of one contract on one day share a file; each reads its own."""
    log = PriceLog(tmp_path)
    log.record(CONTRACT, START_MS, 1.0)
    log.record(CONTRACT, START_MS + 60_000, 2.0)
    log.record(CONTRACT, START_MS + 120_000, 3.0)

    assert log.read(CONTRACT, START_MS + 30_000, START_MS + 90_000) == [
        (START_MS + 60_000, 2.0)
    ]


def test_a_line_cut_short_by_a_crash_is_skipped(tmp_path):
    log = PriceLog(tmp_path)
    log.record(CONTRACT, START_MS, 1.0)
    path = next(tmp_path.rglob("*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"t": 17902806')          # half a line, no newline

    assert log.read(CONTRACT, START_MS, START_MS + 1) == [(START_MS, 1.0)]


def test_a_trade_across_utc_midnight_reads_both_days(tmp_path):
    log = PriceLog(tmp_path)
    midnight = 1790294400000                     # 2026-09-25 00:00 UTC
    log.record(CONTRACT, midnight - 1000, 1.0)
    log.record(CONTRACT, midnight + 1000, 2.0)

    assert [p for _, p in log.read(CONTRACT, midnight - 5000, midnight + 5000)] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


def held(identifier, price):
    return SimpleNamespace(identifier=identifier, market_price_latest=price)


def test_the_recorder_writes_every_held_contract(tmp_path):
    log = PriceLog(tmp_path)
    recorder = PriceRecorder(
        lambda _age: ([held(CONTRACT, 3.6), held("NVDA  260928C00230000", 0.5)], 0.0),
        log,
    )
    assert recorder.tick() == 2
    assert len(list(tmp_path.rglob("*.jsonl"))) == 2


def test_a_contract_bought_again_starts_fresh(tmp_path):
    """Sold, then bought again at the SAME price: the new run is written."""
    log = PriceLog(tmp_path)
    positions = [[held(CONTRACT, 3.6)], [], [held(CONTRACT, 3.6)]]
    recorder = PriceRecorder(lambda _age: (positions.pop(0), 0.0), log)

    recorder.tick()
    recorder.tick()
    recorder.tick()

    # The recorder stamps with the real clock.
    now = int(time.time() * 1000)
    assert len(log.read(CONTRACT, now - DAY, now + DAY)) == 2


def test_a_missing_price_is_not_written(tmp_path):
    log = PriceLog(tmp_path)
    PriceRecorder(lambda _age: ([held(CONTRACT, None)], 0.0), log).tick()
    assert list(tmp_path.rglob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------


def test_the_line_starts_at_the_fill_and_ends_at_the_exit():
    """The whole bug. Fill 3.60 inside a minute that closed 3.20: the line's
    first point is 3.60, not 3.20."""
    entry_ms = START_MS + 10_000
    exit_ms = START_MS + 300_000
    ticks = [(START_MS + 5_000, 9.99),           # before the fill: not this trade
             (START_MS + 20_000, 3.50),
             (START_MS + 40_000, 3.20),
             (START_MS + 400_000, 9.99)]         # after the exit
    lead, path, source = journey.build_path(
        [bar(0, 3.20)], ticks,
        entry_ms=entry_ms, entry_price=3.60,
        end_ms=exit_ms, end_price=2.90,
    )

    assert source == "recorded"
    assert (path[0].time_ms, path[0].price) == (entry_ms, 3.60)
    assert (path[-1].time_ms, path[-1].price) == (exit_ms, 2.90)
    assert [p.price for p in path] == [3.60, 3.50, 3.20, 2.90]


def test_without_recordings_the_middle_is_minute_closes_at_their_end():
    entry_ms = START_MS + 10_000
    exit_ms = START_MS + 150_000
    _lead, path, source = journey.build_path(
        [bar(0, 3.20), bar(1, 3.40), bar(2, 3.10)], [],
        entry_ms=entry_ms, entry_price=3.60,
        end_ms=exit_ms, end_price=2.90,
    )

    assert source == "minute_bars"
    # Bar 0 ends at +60s, bar 1 at +120s; bar 2 ends after the exit.
    assert [(p.time_ms - START_MS, p.price) for p in path] == [
        (10_000, 3.60), (60_000, 3.20), (120_000, 3.40), (150_000, 2.90),
    ]


def test_the_run_up_ends_at_the_fill_so_the_lines_join():
    entry_ms = START_MS + 130_000
    lead, path, _ = journey.build_path(
        [bar(0, 3.0), bar(1, 3.1), bar(2, 3.3)], [],
        entry_ms=entry_ms, entry_price=3.60,
        end_ms=None, end_price=None,
    )

    assert [p.price for p in lead] == [3.0, 3.1, 3.60]
    assert lead[-1] == path[0]


def test_the_best_is_measured_on_the_drawn_line():
    """A minute's high of 4.50 that the recorded prices never show is not the
    best: the story must not name a peak the line does not reach."""
    entry_ms = START_MS + 10_000
    _lead, path, _ = journey.build_path(
        [bar(0, 3.8, h=4.50)], [(START_MS + 20_000, 4.20)],
        entry_ms=entry_ms, entry_price=3.60,
        end_ms=START_MS + 50_000, end_price=2.90,
    )
    result = journey.build(journey.path_as_bars(path), entry_price=3.60)

    assert result.best.price == 4.20
    assert result.worst.price == 2.90
