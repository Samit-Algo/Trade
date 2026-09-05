"""The valid option price increment, and snapping prices onto it.

Pure arithmetic. Nothing here touches the network.

WHY THIS FILE EXISTS: Tiger returns `min_tick` as None on every contract call
this project makes -- see `contract/resolve.py:read_min_tick`. There is no API
source for the increment, so it was MEASURED instead, from prices that real
trades actually happened at. See HANDOVER.md section 3d for the evidence.

The measured answer is $0.01 at every price level. That refutes the widely
quoted "penny under $3.00, nickel at $3.00 and above" convention, which this
project came close to hard-coding: 32,360 observed traded prices across six
symbols include 6,258 prices at or above $3.00 that are NOT on a nickel.

Assuming the nickel band would have made the entry buffer five times larger
than intended on every contract over $3.00, silently.
"""

from __future__ import annotations

import math

#: The measured increment. Overridable via OPTION_TICK_SIZE in .env for a
#: symbol class that turns out to quote more coarsely -- see HANDOVER 3d.
MEASURED_TICK_SIZE = 0.01

#: Said in every response that uses it, so the number is never mistaken for
#: something the broker reported. It is not: Tiger returns min_tick as None.
TICK_SOURCE_NOTE = (
    "measured from 32,360 real traded prices across 6 symbols; Tiger reports "
    "no min_tick. See HANDOVER.md section 3d."
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
