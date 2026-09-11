"""OPTIONAL: a buy-limit buffer that scales with the option's premium.

=============================================================================
 THIS WHOLE FEATURE IS REMOVABLE. To turn it off, set in .env:

     BUFFER_TIERS_ENABLED=false

 To delete it outright: remove this file, the two `buffer_tiers` lines in
 `order/__init__.py`, the `buffer_tiers_enabled` setting in `core/config.py`,
 and the `resolve_buffer_ticks` call in `routes/trade.py`. Nothing else
 references it, and `LIMIT_BUFFER_TICKS` goes back to being the only rule.
=============================================================================

WHY. A flat buffer is the wrong shape. `LIMIT_BUFFER_TICKS=5` is 5 cents
whatever the premium -- 3.1% of a $1.60 option and 0.8% of a $6.00 one. The
cheap contract carries four times the relative headroom for no reason.

THE TIERS, as specified:

    premium        buffer
    < 1.50          0.01     <-- raised to 0.02 by default, see below
    1.50 - 2.50     0.02
    2.50 - 3.50     0.03
    3.50 - 6.00     0.05
    > 6.00          0.10

MEASURED AGAINST 55 REAL FILLED BUYS (paper, 2026-09-09/10):

    scheme              fills    unused headroom
    flat 0.01           42/55    $70
    flat 0.05 (before)  55/55    $273
    tiers as specified  52/55    $190
    tiers, bottom 0.02  55/55    $242

The three misses under the specified tiers were EACH SHORT BY EXACTLY ONE
CENT, and all three sat in the `< 1.50` band:

    NVDA 215 PUT   last 0.89 -> limit 0.90, really filled 0.91
    AAPL 330 CALL  last 0.33 -> limit 0.34, really filled 0.35
    NFLX 76 PUT    last 1.40 -> limit 1.41, really filled 1.42

A missed limit is not a worse fill, it is NO fill -- so those are three trades
that would not have happened, and 38% of the trades analysed were in that
band. Hence `BUFFER_TIER_FLOOR_TICKS`, which defaults to 2 and lifts only the
bottom band. Set it to 1 to run the specification exactly as written.

WHAT THIS IS NOT. The backtest compares each real fill against a hypothetical
limit. It cannot prove a tighter limit would not have changed how the market
responded, and 55 orders across three symbols over two days is directional,
not statistically strong. Run it in DRY_RUN for a session before trusting it.

Buffers are returned in WHOLE TICKS, not dollars, because `apply_buffer` works
in ticks and the tick size is itself configurable.
"""

from __future__ import annotations

#: Upper bound (exclusive) of each premium band, with the buffer in DOLLARS.
#: Ordered low to high; the first band whose bound the premium is under wins.
#: A premium at or above every bound falls to DEFAULT_TOP_TIER_BUFFER.
DEFAULT_TIERS: tuple[tuple[float, float], ...] = (
    (1.50, 0.01),
    (2.50, 0.02),
    (3.50, 0.03),
    (6.00, 0.05),
)

#: Applied above the highest band.
DEFAULT_TOP_TIER_BUFFER = 0.10

#: The lowest buffer any tier may resolve to, in whole ticks. Defaults to 2
#: because at 1 the measured miss rate was 3 in 55, every one of them a
#: one-cent shortfall in the cheapest band. Set BUFFER_TIER_FLOOR_TICKS=1 to
#: run the tiers exactly as specified.
DEFAULT_FLOOR_TICKS = 2


def buffer_dollars_for_premium(
    premium: float,
    tiers: tuple[tuple[float, float], ...] = DEFAULT_TIERS,
    top_tier_buffer: float = DEFAULT_TOP_TIER_BUFFER,
) -> float:
    """Return the tiered buffer for a premium, in dollars.

    Args:
        premium: The option premium the buffer will be added to.
        tiers: (upper bound exclusive, buffer in dollars), lowest bound first.
        top_tier_buffer: Used when the premium is above every bound.

    Returns:
        The buffer in dollars.
    """
    for upper_bound, buffer in tiers:
        if premium < upper_bound:
            return buffer
    return top_tier_buffer


def resolve_buffer_ticks(
    premium: float,
    tick_size: float,
    fallback_ticks: int,
    enabled: bool,
    floor_ticks: int = DEFAULT_FLOOR_TICKS,
) -> tuple[int, str]:
    """Decide how many ticks to add to the buy limit, and say why.

    The reason string is returned rather than logged so it can reach the API
    response: a buffer that changes per trade is invisible otherwise, and an
    unexplained limit price is exactly what makes a surprising fill hard to
    trace afterwards.

    Args:
        premium: The snapped entry premium, in dollars.
        tick_size: The valid price increment.
        fallback_ticks: LIMIT_BUFFER_TICKS, used when tiers are off.
        enabled: False to ignore the tiers entirely.
        floor_ticks: Lowest number of ticks any tier may produce.

    Returns:
        A pair of (whole ticks to add, one sentence saying why).
    """
    if not enabled:
        return fallback_ticks, (
            f"flat {fallback_ticks} tick(s) from LIMIT_BUFFER_TICKS; "
            "premium tiers are off"
        )

    # A tick size of zero or less would make this meaningless, and it is
    # validated at startup -- but this function must not divide by it blindly.
    if tick_size <= 0:
        return fallback_ticks, (
            f"flat {fallback_ticks} tick(s); tick size {tick_size} is unusable"
        )

    buffer = buffer_dollars_for_premium(premium)

    # Round rather than truncate: at a 0.01 tick these divide exactly, but a
    # coarser tick size would silently floor a 0.03 buffer to zero ticks.
    ticks = max(floor_ticks, round(buffer / tick_size))

    reason = (
        f"{ticks} tick(s) = {ticks * tick_size:.2f} for a premium of "
        f"{premium:.2f}, from the premium tiers"
    )
    if ticks > round(buffer / tick_size):
        reason += f" (raised to the {floor_ticks}-tick floor)"

    return ticks, reason
