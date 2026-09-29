"""THE ONLY FILE THAT CAN PLACE AN INDIAN ORDER.

Every `placeorder` call for India is in this file, and each one is preceded
immediately by its guard. If you are reviewing what India can do with your
money, read this file.

TWO GUARDS, BECAUSE A BUY AND AN EXIT ARE DIFFERENT RISKS.

    A BUY opens risk. It is refused while the page's switch is SAFE, and it
    is refused unless OpenAlgo's Analyze mode is ON -- or ALLOW_LIVE is true.
    The mode is read from OpenAlgo right before the send, every time.

    AN EXIT closes risk. The SAFE switch never blocks it: a switch that
    stopped you selling what you hold would be dangerous exactly when you
    most need out. But it must go to the SAME place the position is. A
    position opened in Analyze mode exists only in OpenAlgo's simulator; if
    Analyze were switched off, a "sell" would reach the real Angel account
    and open a real SHORT. So an exit is refused unless the mode now is the
    mode the position was opened in.

Nothing may be inserted between a guard and the send below it.
"""

from __future__ import annotations

from backend.core import armed
from backend.core.safety import LiveTradingBlocked
from backend.services.order import OrderSubmissionError

from .openalgo import STRATEGY, resolve_mode


def _order_id(response, what: str) -> str:
    """The order id from placeorder's reply, or raise with what was said.

    placeorder puts the id at the TOP level -- {"orderid": ..., "status":
    "success"} -- unlike the books, which nest under "data". Reading only
    "data" would report a failure on an order that was in fact placed.
    """
    if not isinstance(response, dict):
        raise OrderSubmissionError(f"{what}: OpenAlgo returned {response!r}.")
    if response.get("status") != "success":
        raise OrderSubmissionError(
            f"{what} was refused. OpenAlgo said: {response.get('message', response)}"
        )
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    order_id = response.get("orderid") or data.get("orderid") or data.get("order_id")
    if not order_id:
        raise OrderSubmissionError(
            f"{what} was accepted but carried no order id: {response!r}. Check "
            "OpenAlgo's order book before retrying -- an order may exist."
        )
    return str(order_id)


def place_entry(client, settings, *, symbol: str, units: int, limit_price: float) -> tuple[str, str]:
    """Send the entry BUY, as a LIMIT.

    Returns:
        (order id, "PAPER" or "LIVE" -- where it was sent).

    Raises:
        LiveTradingBlocked: SAFE, or Analyze OFF without ALLOW_LIVE. Nothing sent.
        OrderSubmissionError: OpenAlgo refused.
    """
    what = f"BUY {units} {symbol} at {limit_price}"

    # THE GUARD. Nothing goes between it and the send.
    if armed.is_dry(settings):
        raise LiveTradingBlocked("India is SAFE, so no order was sent.")
    mode = resolve_mode(client, settings.allow_live)

    response = client.placeorder(
        strategy=STRATEGY, symbol=symbol, exchange=settings.exchange,
        action="BUY", price_type="LIMIT", product=settings.product,
        quantity=int(units), price=limit_price,
    )
    return _order_id(response, what), mode


def assert_exit_allowed(client, settings, opened_in: str) -> str:
    """Refuse an exit that would reach a different place than the position.

    Returns:
        The mode, which equals `opened_in`.

    Raises:
        LiveTradingBlocked: When the mode cannot be read, or has changed.
    """
    now = resolve_mode(client, allow_live=True)  # the SAFE switch is not consulted
    if now != opened_in:
        raise LiveTradingBlocked(
            f"This position was opened in {opened_in} mode but OpenAlgo is now in "
            f"{now} mode. Selling it here would reach the wrong account -- in "
            "the worst case opening a real short. Nothing was sent. Switch "
            "OpenAlgo back, then close it."
        )
    if now == "LIVE" and not settings.allow_live:
        # Unreachable while opened_in is honest, but a live exit is only ever
        # sent for a live position, and live positions need ALLOW_LIVE.
        raise LiveTradingBlocked("ALLOW_LIVE is false, so no live order was sent.")
    return now


def place_exit(client, settings, *, symbol: str, units: int, opened_in: str,
               price_type: str, price: float | None = None,
               trigger_price: float | None = None) -> str:
    """Send a SELL that closes a held position: a stop, a limit, or a market.

    Args:
        opened_in: "PAPER" or "LIVE", the mode the position was opened in.
        price_type: "SL" (a stop resting at the exchange), "LIMIT" or "MARKET".

    Returns:
        The order id.

    Raises:
        LiveTradingBlocked: The mode changed since the position was opened.
        OrderSubmissionError: OpenAlgo refused.
    """
    what = f"SELL {units} {symbol} ({price_type})"
    order = dict(
        strategy=STRATEGY, symbol=symbol, exchange=settings.exchange,
        action="SELL", price_type=price_type, product=settings.product,
        quantity=int(units),
    )
    if price is not None:
        order["price"] = price
    if trigger_price is not None:
        order["trigger_price"] = trigger_price

    # THE GUARD. Nothing goes between it and the send.
    assert_exit_allowed(client, settings, opened_in)

    response = client.placeorder(**order)
    return _order_id(response, what)
