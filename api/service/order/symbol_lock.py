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

from tigeropen.common.consts import SecurityType

from api.service.core.broker import OPEN_ORDERS_LIMITER
from api.service.position import list_option_positions


#: Underlyings currently inside place_bracketed_trade in THIS process, held
#: from the moment the check passes until the request finishes. Without it two
#: near-simultaneous requests both read "nothing open" from the broker before
#: either has submitted, and both proceed.
_in_flight: set[str] = set()
_in_flight_guard = threading.Lock()


class SymbolBusy(Exception):
    """Raised when the underlying already has a live order or position."""


def _underlying_of(order) -> str:
    """Read the underlying from an open order.

    Tiger renders the contract as "QQQ  260918C00360000/OPT/USD". The
    underlying is the first whitespace-separated token of the part before the
    first slash.

    Args:
        order: One raw order from get_open_orders.

    Returns:
        The underlying symbol in upper case, or "" when it cannot be read.
    """
    contract_text = str(getattr(order, "contract", "")).split("/")[0]
    return contract_text.strip().split(" ")[0].strip().upper()


def find_open_orders_for(underlying: str, trade_client, account: str | None):
    """List live orders on the broker's book for one underlying.

    Args:
        underlying: The underlying symbol.
        trade_client: A tigeropen TradeClient.
        account: Account ID, or None for the configured default.

    Returns:
        A list of short descriptions, one per live order.
    """
    OPEN_ORDERS_LIMITER.wait()
    raw = trade_client.get_open_orders(account=account, sec_type=SecurityType.OPT)

    wanted = underlying.strip().upper()
    descriptions = []
    for order in raw or []:
        if _underlying_of(order) != wanted:
            continue
        contract_text = str(getattr(order, "contract", "")).split("/")[0]
        status = str(getattr(order, "status", "")).split(".")[-1]
        action = str(getattr(order, "action", "") or "")
        descriptions.append(f"{contract_text} {action} ({status})")

    return descriptions


def find_positions_for(underlying: str, trade_client):
    """List held option positions for one underlying.

    Args:
        underlying: The underlying symbol.
        trade_client: A tigeropen TradeClient.

    Returns:
        A list of short descriptions, one per held position.
    """
    wanted = underlying.strip().upper()
    return [
        f"{position.describe()} x{position.quantity:g}"
        for position in list_option_positions(trade_client)
        if position.underlying.strip().upper() == wanted
    ]


def claim_symbol(underlying: str, trade_client, account: str | None) -> None:
    """Refuse the trade if this underlying is already busy, else reserve it.

    The local reservation is taken FIRST and the broker read happens inside
    it, so two requests for the same symbol cannot both be reading at once.

    Args:
        underlying: The underlying about to be traded.
        trade_client: A tigeropen TradeClient.
        account: Account ID, or None for the configured default.

    Raises:
        SymbolBusy: When an order or position for this underlying is live.
    """
    wanted = underlying.strip().upper()

    with _in_flight_guard:
        if wanted in _in_flight:
            raise SymbolBusy(
                f"Another {wanted} trade is being placed right now. "
                "Wait for it to finish."
            )
        _in_flight.add(wanted)

    # From here the reservation is held, so any failure must release it.
    try:
        working = find_open_orders_for(wanted, trade_client, account)
        if working:
            raise SymbolBusy(
                f"{wanted} already has an order on the book, so no new "
                f"{wanted} trade was placed: {'; '.join(working)}. "
                "Cancel it or wait for it to fill or be closed."
            )

        held = find_positions_for(wanted, trade_client)
        if held:
            raise SymbolBusy(
                f"{wanted} is already held, so no new {wanted} trade was "
                f"placed: {'; '.join(held)}. Close it first."
            )
    except Exception:
        release_symbol(wanted)
        raise


def release_symbol(underlying: str) -> None:
    """Give the underlying back, so a later request may trade it.

    Releases only the local in-process reservation. An order that actually
    reached the broker keeps the symbol busy through the broker reads above,
    which is the intended behaviour -- the position is what blocks, not this.

    Args:
        underlying: The underlying to release.
    """
    with _in_flight_guard:
        _in_flight.discard(underlying.strip().upper())
