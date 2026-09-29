"""What a contract did between entry and exit.

THE QUESTION THIS ANSWERS. "It reached 1.03 and I had the take profit at
1.05, but I never knew." Realised P&L cannot say that: a trade that ran to
within a whisker of its target and reversed reports the same number as one
that drifted sideways. The difference is the whole signal about whether the
take profit is set too far out, and until now it was unrecorded.

HOW. Minute bars carry `high` and `low`. The peak after entry is
max(high), the trough is min(low), and "how close did it get" is that peak
measured against the distance from entry to the take profit.

PURE. Nothing here fetches anything. Bars go in, numbers come out, so the
arithmetic is testable without a network and cannot fail mid-request.

WHY THE PEAK CAN EXCEED A TAKE PROFIT THAT NEVER FIRED. The bracket legs
rest with the broker and trigger on the quote it sees. A one-minute high can
come from a print a resting order never had the chance to match. A peak
above the take profit is therefore not proof the leg misbehaved, and the
caller should say so rather than let it read as a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.service.market.bars import Bar


@dataclass(frozen=True)
class Extreme:
    """One end of the range, and when it happened.

    Attributes:
        price: The premium.
        time_ms: The bar it occurred in.
        percent_from_entry: Signed move from entry, in percent.
    """

    price: float
    time_ms: int
    percent_from_entry: float


@dataclass(frozen=True)
class Journey:
    """The life of one trade, derived from its bars.

    Attributes:
        bars: The bars themselves, oldest first, for drawing.
        entry_price: What it was bought at.
        quantity: Contracts.
        multiplier: Shares per contract, for money.
        take_profit_price: The level aimed at, when one was set.
        stop_loss_price: The level defended, when one was set.
        best: Highest premium reached after entry.
        worst: Lowest premium reached after entry.
        progress_to_take_profit: How far the peak got towards the take
            profit, 0-1 and capped at 1. None when no take profit was set.
        drawdown_to_stop_loss: The same towards the stop loss.
        high_water_pnl: What the position was worth at its best, in money.
        low_water_pnl: And at its worst.
        touched_take_profit: Whether a bar's high reached the take profit.
        touched_stop_loss: Whether a bar's low reached the stop loss.
    """

    bars: tuple[Bar, ...]
    entry_price: float
    quantity: int
    multiplier: float
    take_profit_price: float | None
    stop_loss_price: float | None
    best: Extreme | None
    worst: Extreme | None
    progress_to_take_profit: float | None
    drawdown_to_stop_loss: float | None
    high_water_pnl: float | None
    low_water_pnl: float | None
    touched_take_profit: bool
    touched_stop_loss: bool

    @property
    def has_bars(self) -> bool:
        return bool(self.bars)


@dataclass(frozen=True)
class PricePoint:
    """One price at one moment, for drawing."""

    time_ms: int
    price: float


#: A minute bar's close is the price at the END of its minute.
BAR_MS = 60_000


def build_path(
    bars,
    ticks,
    *,
    entry_ms: int,
    entry_price: float,
    end_ms: int | None,
    end_price: float | None,
) -> tuple[list[PricePoint], list[PricePoint], str]:
    """The line a journey is drawn as: the fill, what happened, the exit.

    WHY A PATH AND NOT THE BARS. A bar's close is the price at the last
    second of its minute. The fill and the exit happen inside a minute, so a
    line of closes never passed through either, and their dots sat off it by
    whatever the price did in the rest of the minute. This line STARTS at
    the fill and ENDS at the exit, so both dots are on it by construction.

    In between it uses the recorded prices (price_log.py) when there are
    any, and the minute closes when there are not -- a trade from before the
    recorder, or one held while the backend was down.

    Args:
        bars: Minute bars around the trade, any order.
        ticks: Recorded (time_ms, price) pairs for the contract.
        entry_ms: When it filled.
        entry_price: The fill.
        end_ms: The exit, or now for an open trade. None when unknown.
        end_price: The exit fill, or the current price. None when unknown.

    Returns:
        (lead, path, source). `lead` is the minutes BEFORE the fill, ending
        at the fill so the two lines join -- context, not part of the trade.
        `path` runs fill to exit. `source` is "recorded" or "minute_bars".
    """
    ordered = sorted(bars, key=lambda b: b.time_ms)

    # Stamped at the bar's END, which is when its close was the price.
    lead = [
        PricePoint(b.time_ms + BAR_MS, b.close)
        for b in ordered
        if b.time_ms + BAR_MS <= entry_ms
    ]
    if lead:
        lead.append(PricePoint(entry_ms, entry_price))

    def inside(t: int) -> bool:
        return t > entry_ms and (end_ms is None or t < end_ms)

    between = [PricePoint(int(t), float(p)) for t, p in ticks if inside(int(t))]
    source = "recorded"
    if not between:
        source = "minute_bars"
        between = [
            PricePoint(b.time_ms + BAR_MS, b.close)
            for b in ordered
            if inside(b.time_ms + BAR_MS)
        ]
    between.sort(key=lambda pt: pt.time_ms)

    path = [PricePoint(entry_ms, entry_price)] + between
    if end_ms is not None and end_price is not None and end_ms > entry_ms:
        path.append(PricePoint(end_ms, end_price))

    return lead, path, source


def path_as_bars(path: list[PricePoint]) -> list[Bar]:
    """The path in the shape build() measures, one flat bar per point.

    So the best, the worst and "did it touch the take profit" are measured
    on the SAME line the chart draws. Measured on the bars' highs instead,
    the story could name a peak the line never reaches.
    """
    return [
        Bar(time_ms=pt.time_ms, open=pt.price, high=pt.price,
            low=pt.price, close=pt.price, volume=0)
        for pt in path
    ]


def _percent(entry: float, price: float) -> float:
    """Signed move from entry, in percent."""
    if entry <= 0:
        return 0.0
    return (price - entry) / entry * 100.0


def _money(entry: float, price: float, quantity: int, multiplier: float) -> float:
    """What the move is worth, before commission.

    Rounded to cents: binary floats turn 0.07 * 2 * 100 into 13.999999999,
    and a panel showing $13.999999999 reads as a bug in the arithmetic.
    """
    return round((price - entry) * quantity * multiplier, 2)


def _fraction(entry: float, reached: float, target: float | None) -> float | None:
    """How far `reached` got from `entry` towards `target`.

    Args:
        entry: The starting price.
        reached: The extreme reached.
        target: The level being measured against, or None.

    Returns:
        0 to 1, capped at 1, or None when there is no target or the target
        sits at entry (which would divide by zero and means nothing anyway).
    """
    if target is None:
        return None

    distance = target - entry
    if distance == 0:
        return None

    travelled = (reached - entry) / distance
    if travelled <= 0:
        return 0.0
    return min(1.0, travelled)


def build(
    bars,
    *,
    entry_price: float,
    quantity: int = 1,
    multiplier: float = 100.0,
    take_profit_price: float | None = None,
    stop_loss_price: float | None = None,
) -> Journey:
    """Derive the journey from a trade's bars.

    Args:
        bars: Minute bars covering the trade, oldest first. May be empty.
        entry_price: The fill.
        quantity: Contracts held.
        multiplier: Shares per contract.
        take_profit_price: The take-profit level, when one was set.
        stop_loss_price: The stop-loss level, when one was set.

    Returns:
        The journey. With no bars, every derived field is None rather than
        zero -- "not known" and "did not move" are different answers, and
        showing 0.00 for the first would be a lie.
    """
    ordered = tuple(sorted(bars, key=lambda b: b.time_ms))

    if not ordered or entry_price <= 0:
        return Journey(
            bars=ordered,
            entry_price=entry_price,
            quantity=quantity,
            multiplier=multiplier,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            best=None,
            worst=None,
            progress_to_take_profit=None,
            drawdown_to_stop_loss=None,
            high_water_pnl=None,
            low_water_pnl=None,
            touched_take_profit=False,
            touched_stop_loss=False,
        )

    peak_bar = max(ordered, key=lambda b: b.high)
    trough_bar = min(ordered, key=lambda b: b.low)

    best = Extreme(
        price=peak_bar.high,
        time_ms=peak_bar.time_ms,
        percent_from_entry=_percent(entry_price, peak_bar.high),
    )
    worst = Extreme(
        price=trough_bar.low,
        time_ms=trough_bar.time_ms,
        percent_from_entry=_percent(entry_price, trough_bar.low),
    )

    return Journey(
        bars=ordered,
        entry_price=entry_price,
        quantity=quantity,
        multiplier=multiplier,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        best=best,
        worst=worst,
        progress_to_take_profit=_fraction(
            entry_price, peak_bar.high, take_profit_price
        ),
        drawdown_to_stop_loss=_fraction(
            entry_price, trough_bar.low, stop_loss_price
        ),
        high_water_pnl=_money(entry_price, peak_bar.high, quantity, multiplier),
        low_water_pnl=_money(entry_price, trough_bar.low, quantity, multiplier),
        # Touching is >= for a take profit and <= for a stop loss: both are
        # levels the price has to REACH, from opposite directions.
        touched_take_profit=(
            take_profit_price is not None and peak_bar.high >= take_profit_price
        ),
        touched_stop_loss=(
            stop_loss_price is not None and trough_bar.low <= stop_loss_price
        ),
    )
