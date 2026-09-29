"""One trade per underlying at a time.

WHY THIS EXISTS. The four locks in main.py all ask "may this caller trade at
all?". None of them asks "is there already a trade running on THIS symbol?".
Clicking CALL twice on the same chart is one keystroke, and without this the
second click opens a second position at whatever strike the price has moved
to -- doubling the size of a trade that was meant to be one.

WHAT COUNTS AS BUSY. Both halves of an order's life, because the gap between
them is exactly when a second click is most likely:

    1. SUBMITTED, NOT YET FILLED -- sitting on the broker's book. No position
       exists yet, so a check that only read positions would wave it through.
    2. FILLED -- held as a position, until it is closed or expires.

WHAT THIS IS NOT. Not a transactional lock. The broker is read over the
network, so between the read here and the submit that follows there is a
window in which a second request could pass the same check. It is narrow, and
the local in-flight guard below closes it for requests reaching THIS process;
it cannot close it against an order placed from the Tiger app at the same
moment. Treat this as protection against a double click, not as a guarantee
of exclusivity.
"""

from __future__ import annotations

import threading


#: Underlyings currently inside place_bracketed_trade in THIS process, held
#: from the moment the check passes until the request finishes. Without it two
#: near-simultaneous requests both read "nothing open" from the broker before
#: either has submitted, and both proceed.
_in_flight: set[tuple[str, str]] = set()
_in_flight_guard = threading.Lock()


class SymbolBusy(Exception):
    """Raised when the underlying already has a live order or position."""


def find_open_orders_for(underlying: str, market) -> list[str]:
    """List live orders on the broker's book for one underlying.

    Args:
        underlying: The underlying symbol.
        market: The Market the trade is for.

    Returns:
        A list of short descriptions, one per live order.
    """
    wanted = underlying.strip().upper()
    return [
        f"{order.identifier} {order.action} ({order.status})"
        for order in market.open_orders()
        if order.underlying == wanted
    ]


def find_positions_for(underlying: str, market) -> list[str]:
    """List held option positions for one underlying.

    Args:
        underlying: The underlying symbol.
        market: The Market the trade is for.

    Returns:
        A list of short descriptions, one per held position.
    """
    wanted = underlying.strip().upper()
    return [
        f"{position.describe()} x{position.quantity:g}"
        for position in market.positions()
        if position.underlying.strip().upper() == wanted
    ]


def claim_symbol(underlying: str, market) -> None:
    """Refuse the trade if this underlying is already busy, else reserve it.

    The local reservation is taken FIRST and the broker read happens inside
    it, so two requests for the same symbol cannot both be reading at once.

    Args:
        underlying: The underlying about to be traded.
        market: The Market the trade is for.

    Raises:
        SymbolBusy: When an order or position for this underlying is live.
    """
    wanted = underlying.strip().upper()
    key = (market.profile.id, wanted)

    with _in_flight_guard:
        if key in _in_flight:
            raise SymbolBusy(
                f"Another {wanted} trade is being placed right now. "
                "Wait for it to finish."
            )
        _in_flight.add(key)

    # From here the reservation is held, so any failure must release it.
    try:
        working = find_open_orders_for(wanted, market)
        if working:
            raise SymbolBusy(
                f"{wanted} already has an order on the book, so no new "
                f"{wanted} trade was placed: {'; '.join(working)}. "
                "Cancel it or wait for it to fill or be closed."
            )

        held = find_positions_for(wanted, market)
        if held:
            raise SymbolBusy(
                f"{wanted} is already held, so no new {wanted} trade was "
                f"placed: {'; '.join(held)}. Close it first."
            )
    except Exception:
        release_symbol(wanted, market.profile.id)
        raise


def release_symbol(underlying: str, market_id: str = "US") -> None:
    """Give the underlying back, so a later request may trade it.

    Releases only the local in-process reservation. An order that actually
    reached the broker keeps the symbol busy through the broker reads above,
    which is the intended behaviour -- the position is what blocks, not this.

    Args:
        underlying: The underlying to release.
        market_id: The market it was claimed in.
    """
    with _in_flight_guard:
        _in_flight.discard((market_id, underlying.strip().upper()))
