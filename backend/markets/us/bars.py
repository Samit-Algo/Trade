"""Minute bars for one US option contract, from Tiger.

The Bar shape, and what a bar can and cannot tell you, are in
backend/services/market/bars.py.
"""

from __future__ import annotations

from datetime import datetime

from backend.services.market.bars import Bar

from .broker import OPTION_BARS_LIMITER


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
