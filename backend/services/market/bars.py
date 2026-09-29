"""Minute bars for one option contract, over one window.

WHY THIS EXISTS. Everything the service records happens at ENTRY. The audit
log holds the price a contract was bought at and the bracket it was given,
and then nothing -- so a trade that ran most of the way to its take profit
and came back looks exactly like one that never moved.

The answer is already on the wire. Tiger serves one-minute OHLC for options,
and `high` is the peak the contract reached. Fetching the bars for the life
of a trade turns "still open, +0.06" into "reached 3.46 at 20:48, which was
96% of the way to the take profit, then fell away".

WHY NOT A MONITORING LOOP. This service has no background task and this does
not add one. Bars after the fact answer the same question as polling, cost
one request when a human opens a panel, and cannot interfere with an order.

WHAT A BAR CANNOT TELL YOU. A minute is a long time. A spike that lasted
four seconds inside a minute IS in that minute's `high`, but nothing records
where within the minute it happened, and a print between two bars is not
there at all. Callers should say "one-minute bars" rather than imply tick
resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.core.broker import OPTION_BARS_LIMITER

#: Tiger returns at most this many bars per request, and silently truncates
#: rather than erroring. A window wider than this loses its OLDEST bars,
#: which for a trade journey is the entry -- so callers that care pass a
#: window narrow enough to fit.
MAX_BARS = 300


@dataclass(frozen=True)
class Bar:
    """One minute of trading in a contract.

    Attributes:
        time_ms: Bar open, milliseconds since epoch UTC, as Tiger sends it.
        open: First trade of the minute.
        high: Highest trade. THE reason this module exists.
        low: Lowest trade.
        close: Last trade.
        volume: Contracts traded in the minute.
    """

    time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def moment(self) -> datetime:
        """The bar's open, as an aware UTC datetime."""
        return datetime.fromtimestamp(self.time_ms / 1000, tz=timezone.utc)


def _to_int_ms(moment: datetime) -> int:
    """Convert an aware datetime to Tiger's millisecond timestamps.

    Args:
        moment: Any aware datetime.

    Returns:
        Milliseconds since epoch.
    """
    return int(moment.timestamp() * 1000)


def fetch_minute_bars(
    quote_client, identifier: str, *, begin: datetime, end: datetime
) -> list[Bar]:
    """Read one contract's minute bars over a window.

    Never raises. The caller is drawing a chart for a human, and a trade that
    cannot be charted is a missing picture, not a failure -- the panel says
    so and the rest of the page carries on.

    Args:
        quote_client: Tiger's quote client.
        identifier: The OCC contract identifier.
        begin: Window start, aware. Usually the fill.
        end: Window end, aware. The exit, or now for an open position.

    Returns:
        The bars, oldest first. Empty when there are none, when the request
        failed, or when the window is inverted.
    """
    if begin >= end:
        return []

    OPTION_BARS_LIMITER.wait()

    try:
        from tigeropen.common.consts import BarPeriod, Market

        frame = quote_client.get_option_bars(
            identifiers=[identifier],
            begin_time=_to_int_ms(begin),
            end_time=_to_int_ms(end),
            period=BarPeriod.ONE_MINUTE,
            market=Market.US,
        )
    except Exception:
        return []

    return _rows_to_bars(frame)


def _rows_to_bars(frame) -> list[Bar]:
    """Turn Tiger's DataFrame into Bars, dropping anything unusable.

    A contract with no history comes back as an empty LIST rather than an
    empty DataFrame, so testing `.empty` alone raises AttributeError -- the
    same trap `fetch_last_traded_close` documents.

    Args:
        frame: Whatever get_option_bars returned.

    Returns:
        The bars, oldest first.
    """
    if frame is None or isinstance(frame, list):
        return []
    if getattr(frame, "empty", True):
        return []

    needed = {"time", "open", "high", "low", "close"}
    if not needed.issubset(set(frame.columns)):
        return []

    bars: list[Bar] = []
    for row in frame.to_dict("records"):
        try:
            bar = Bar(
                time_ms=int(row["time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume") or 0),
            )
        except (TypeError, ValueError):
            # One malformed row must not lose the whole chart.
            continue

        # A bar with no range and no volume is a placeholder for a minute
        # that did not trade. Keeping it would draw a flat line through a
        # gap and imply a price that was never quoted.
        if bar.volume == 0 and bar.high == bar.low == bar.open == bar.close:
            continue

        bars.append(bar)

    bars.sort(key=lambda b: b.time_ms)
    return bars
