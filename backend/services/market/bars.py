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

