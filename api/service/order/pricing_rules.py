"""The rules that turn a premium into a price and a size.

Pure arithmetic. Nothing here touches the network and nothing here can send
anything, so all of it is testable offline.

Three concerns:

    Ticks           snapping a price onto the valid increment, and the
                    MEASURED tick size -- Tiger returns min_tick as None
    Buffer tiers    OPTIONAL: how far above the last trade to set the buy
                    limit, scaled by premium instead of flat
    Quantity tiers  OPTIONAL: how many contracts, scaled by premium so the
                    CASH per trade is roughly even

Both tier features are removable; each says how at the top of its section.
"""

from __future__ import annotations

import math


# --------------------------------------------------------------------------
# TICKS
# --------------------------------------------------------------------------

#: The measured increment. Overridable via OPTION_TICK_SIZE in .env for a
#: symbol class that turns out to quote more coarsely -- see HANDOVER 3d.
MEASURED_TICK_SIZE = 0.01

#: Said in every response that uses it, so the number is never mistaken for
#: something the broker reported. It is not: Tiger returns min_tick as None.
TICK_SOURCE_NOTE = (
    "measured from 32,360 real traded prices across 6 symbols; Tiger reports "
    "no min_tick. See scrap/HANDOVER.md section 3d."
)

#: Prices are compared in whole ticks, so the arithmetic is integer and exact.
#: Floating point makes 8.05 / 0.05 = 161.00000000000003, and a naive modulo
#: on that reports a perfectly legal price as invalid.
_EPSILON = 1e-9


class TickError(Exception):
    """A price cannot be placed on the tick grid."""


def ticks_in(price: float, tick_size: float) -> float:
    """Return how many ticks a price is, as an exact-as-possible float.

    Args:
        price: The price.
        tick_size: The increment.

    Returns:
        price / tick_size, nudged so that floating point noise does not push
        a price that is exactly on the grid to just under or just over it.
    """
    if tick_size <= 0:
        raise TickError(f"Tick size must be positive, got {tick_size!r}.")
    return price / tick_size


def is_on_tick(price: float, tick_size: float) -> bool:
    """True when a price sits exactly on the grid.

    Args:
        price: The price to test.
        tick_size: The increment.

    Returns:
        Whether the price is a whole number of ticks.
    """
    count = ticks_in(price, tick_size)
    return abs(count - round(count)) < 1e-6


def snap_nearest(price: float, tick_size: float) -> float:
    """Snap a price to the closest valid increment.

    Used on the caller's own entry price: it is what they said they wanted, so
    the least surprising thing is the nearest legal value.

    Args:
        price: The price to snap.
        tick_size: The increment.

    Returns:
        The nearest price on the grid.
    """
    return _clean(round(ticks_in(price, tick_size)) * tick_size)


def snap_up(price: float, tick_size: float) -> float:
    """Snap a price UP to a valid increment, leaving it alone if already valid.

    Used for a take-profit. Rounding a profit target down would sell for less
    than was asked for.

    Args:
        price: The price to snap.
        tick_size: The increment.

    Returns:
        The price at or above `price` that sits on the grid.
    """
    count = ticks_in(price, tick_size)
    return _clean(math.ceil(count - _EPSILON) * tick_size)


def snap_down(price: float, tick_size: float) -> float:
    """Snap a price DOWN to a valid increment, leaving it alone if already valid.

    Used for a stop loss. Rounding a stop up would trigger it sooner, and at a
    worse price, than was asked for.

    Args:
        price: The price to snap.
        tick_size: The increment.

    Returns:
        The price at or below `price` that sits on the grid.
    """
    count = ticks_in(price, tick_size)
    return _clean(math.floor(count + _EPSILON) * tick_size)


def apply_buffer(price: float, tick_size: float, buffer_ticks: int) -> float:
    """Add whole ticks to a price that is already on the grid.

    The entry buffer. Adding ticks rather than a fixed number of cents keeps
    the buffer meaningful at both ends of the price range: one tick is one tick
    whether the contract costs $0.30 or $114.12.

    Args:
        price: A price already snapped to the grid.
        tick_size: The increment.
        buffer_ticks: How many ticks to add. Zero is allowed.

    Returns:
        The buffered price, still on the grid.

    Raises:
        TickError: If buffer_ticks is negative.
    """
    if buffer_ticks < 0:
        raise TickError(
            f"buffer_ticks must be zero or more, got {buffer_ticks}. "
            "A negative buffer would place a BUY further from the market, "
            "which is the opposite of what the buffer is for."
        )
    return _clean(price + buffer_ticks * tick_size)


def _clean(price: float) -> float:
    """Round away the floating point dust a tick multiplication leaves behind.

    0.31 comes out of 31 * 0.01 as 0.31000000000000005. Sending that to a
    broker is asking for a rejection over a rounding artefact, so every price
    leaving this module is rounded to four decimal places -- finer than any
    real increment, coarse enough to erase the noise.

    Args:
        price: The computed price.

    Returns:
        The price without the dust.
    """
    return round(price, 4)


# --------------------------------------------------------------------------
# BUFFER_TIERS
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# QUANTITY_TIERS
# --------------------------------------------------------------------------

#: Upper bound (exclusive) of each premium band, with the quantity to trade.
#: Ordered low to high; the first band whose bound the premium is under wins.
#: A premium at or above every bound falls to DEFAULT_TOP_TIER_QUANTITY.
DEFAULT_TIERS: tuple[tuple[float, int], ...] = (
    (1.00, 5),
    (1.60, 4),
    (2.65, 3),
    (4.00, 2),
)

#: Applied above the highest band.
DEFAULT_TOP_TIER_QUANTITY = 1


def quantity_for_premium(
    premium: float,
    tiers: tuple[tuple[float, int], ...] = DEFAULT_TIERS,
    top_tier_quantity: int = DEFAULT_TOP_TIER_QUANTITY,
) -> int:
    """Return the tiered quantity for a premium, in whole contracts.

    Args:
        premium: The option premium, per share.
        tiers: (upper bound exclusive, quantity), lowest bound first.
        top_tier_quantity: Used when the premium is above every bound.

    Returns:
        The number of contracts to trade.
    """
    for upper_bound, quantity in tiers:
        if premium < upper_bound:
            return quantity
    return top_tier_quantity


def resolve_quantity(
    premium: float,
    fallback_quantity: int,
    enabled: bool,
    tiers: tuple[tuple[float, int], ...] = DEFAULT_TIERS,
    top_tier_quantity: int = DEFAULT_TOP_TIER_QUANTITY,
) -> tuple[int, str]:
    """Decide how many contracts to trade, and say why.

    The reason string is returned rather than logged so it can reach the API
    response: a quantity that changes per trade is invisible otherwise, and an
    unexplained size is exactly what makes a surprising fill hard to trace
    afterwards.

    Args:
        premium: The snapped entry premium, in dollars.
        fallback_quantity: TRADE_QUANTITY, used when tiers are off.
        enabled: False to ignore the tiers entirely.
        tiers: (upper bound exclusive, quantity), lowest bound first.
        top_tier_quantity: Used when the premium is above every bound.

    Returns:
        The quantity, and one sentence explaining it.
    """
    if not enabled:
        return fallback_quantity, (
            f"{fallback_quantity} from TRADE_QUANTITY; premium tiers are off."
        )

    quantity = quantity_for_premium(premium, tiers, top_tier_quantity)

    band = _describe_band(premium, tiers)
    return quantity, (
        f"{quantity} from the premium tiers: {premium:.2f} falls in {band}."
    )


def _describe_band(
    premium: float, tiers: tuple[tuple[float, int], ...]
) -> str:
    """Name the band a premium falls in, for the reason string.

    Args:
        premium: The premium being placed.
        tiers: The bands in force.

    Returns:
        A human-readable band, e.g. "1.00-1.60" or "4.00 and above".
    """
    lower = 0.0
    for upper_bound, _quantity in tiers:
        if premium < upper_bound:
            return f"{lower:.2f}-{upper_bound:.2f}"
        lower = upper_bound
    return f"{lower:.2f} and above"
