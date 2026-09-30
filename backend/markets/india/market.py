"""NIFTY options through Angel One via OpenAlgo, as a `Market`.

What each piece does:

    openalgo.py   the client, and whether an order would be real
    contract.py   which contract: expiry, strike, lot and tick
    data.py       the index, an option's last trade, minute bars
    books.py      the order, trade and position books, read into the
                  neutral shapes -- in LOTS, as the US counts contracts
    submit.py     THE ONLY FILE THAT PLACES AN INDIAN ORDER
    exits.py      the stop loss and the take profit, one on the book at a time

READY ONLY WITH A KEY, AND WHEN SWITCHED ON. Until OPENALGO_API_KEY is set in
config/india.env, or while India is switched off on the Settings page, the
market answers its settings and switches, and every broker call raises
MarketNotReady -- nothing is guessed, nothing is sent, OpenAlgo is not asked.
"""

from __future__ import annotations

import threading
from datetime import date, time
from zoneinfo import ZoneInfo

from backend.core import market_switch
from backend.core.time_brackets import Session
from backend.services.order import (
    BracketLegs,
    FillOutcome,
    OrderSubmissionError,
    calculate_actual_cash,
    classify_fill,
    estimate_cost,
    is_terminal_status,
    validate_bracket_prices,
)

from ..base import Market, MarketNotReady, MarketProfile
from . import contract as contracts
from . import data
from .books import (
    STATUS,
    WORKING,
    fills_by_order,
    read_rows,
    to_broker_order,
    to_option_position,
)
from .exits import ACTIVE, ExitManager
from .openalgo import STRATEGY, build_client, resolve_mode, unwrap
from .submit import place_entry, place_exit

#: The NSE F&O session, in IST.
NSE_SESSION = Session(ZoneInfo("Asia/Kolkata"), time(9, 15), time(15, 30))

PROFILE = MarketProfile(
    id="IN",
    name="India options (Angel One)",
    currency="INR",
    currency_symbol="₹",
    session=NSE_SESSION,
    # Quantities here count lots and every order carries its lot as its
    # multiplier. This is only the fallback for an order that does not.
    contract_multiplier=1.0,
)


def _not_ready(what: str):
    if not market_switch.is_on(PROFILE.id):
        raise MarketNotReady(
            f"India cannot {what}: India is switched off on the Settings page. "
            "Switch it on there to use it. Nothing was sent."
        )
    raise MarketNotReady(
        f"India cannot {what}: OPENALGO_API_KEY is blank in config/india.env. "
        "Its settings, symbols and switches work; trading does not."
    )


def _confirmed(estimate, input_function) -> bool:
    """The caller must type the exact cash figure -- the route does it for HTTP."""
    typed = str(input_function(f"Type {estimate.total_cash:.2f} to confirm: ") or "")
    return typed.strip() == f"{estimate.total_cash:.2f}"


class IndiaMarket(Market):
    """NIFTY options through Angel One via OpenAlgo."""

    profile = PROFILE

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._client = None
        self._lots: dict[str, int] = {}
        self.exits = ExitManager(
            self,
            poll_seconds=settings.exit_poll_seconds,
            stuck_seconds=settings.stuck_exit_seconds,
            slippage_ticks=settings.exit_slippage_ticks,
        )

    @property
    def ready(self) -> bool:
        """Has a key, and is switched on. Read each time: the switch can
        change while the server runs."""
        return bool(self.settings.openalgo_api_key) and market_switch.is_on(self.profile.id)

    def recording_now(self) -> bool:
        """Record prices only in NSE hours. Nothing moves outside them, and
        OpenAlgo is often not running then -- asking would only fail."""
        return self.ready and data.session_is_open(self.profile.session)

    def cannot_switch_off(self) -> str | None:
        """Refuse to switch off while this backend is watching a trade's exits.

        India's take profit is watched here, not an order at the broker.
        Switched off, nothing would watch it: the stop loss would still be at
        the exchange, but the take profit would never fire.
        """
        watched = self._watched()
        if not watched:
            return None
        lines = [
            f"{t['symbol']}: the take profit {self._money(t.get('take_profit'))} will NOT be "
            f"watched; only the stop loss {self._money(t.get('stop_loss'))} at the exchange "
            "protects it."
            for t in watched
        ]
        return (
            "Still open, with its take profit watched by this backend:\n"
            + "\n".join(lines)
            + "\nClose it first -- or switch off anyway and switch India on again "
            "before you want the take profit watched."
        )

    def _watched(self) -> list[dict]:
        """The trades whose exits this backend is watching, one per contract."""
        seen: dict[str, dict] = {}
        for trade in self.exits.trades():
            if trade["status"] in ACTIVE:
                seen.setdefault(trade["symbol"], trade)
        return [seen[symbol] for symbol in sorted(seen)]

    def _money(self, amount) -> str:
        return "(none)" if amount in (None, "") else self.profile.money(float(amount))

    # -- plumbing -----------------------------------------------------------

    @property
    def client(self):
        """The OpenAlgo client, built on first use."""
        with self._lock:
            if self._client is None:
                self._client = build_client(self.settings)
            return self._client

    def _today(self):
        return self.profile.today()

    def _lot(self, symbol: str) -> int:
        """A contract's lot, from the symbol master, remembered for the day."""
        wanted = symbol.strip().upper()
        with self._lock:
            if wanted in self._lots:
                return self._lots[wanted]
        try:
            found = unwrap(self.client.symbol(symbol=wanted, exchange=self.settings.exchange),
                           f"The symbol lookup for {wanted}")
            lot = int(float(found.get("lotsize") or 0)) or self.settings.lot_size
        except Exception:  # noqa: BLE001 -- the configured lot is the best answer left
            lot = self.settings.lot_size
        with self._lock:
            self._lots[wanted] = lot
        return lot

    def start(self) -> None:
        # Started whenever there is a key, even while switched off: it asks
        # OpenAlgo nothing until a trade is being watched, and a trade can
        # only be placed once India is switched on.
        if self.settings.openalgo_api_key:
            self.exits.start()

    def stop(self) -> None:
        self.exits.stop()

    def planned_exits(self, order_id) -> dict | None:
        for trade in self.exits.trades():
            if trade["entry_order_id"] == str(order_id):
                return {"take_profit": trade["take_profit"], "stop_loss": trade["stop_loss"]}
        return None

    def alerts(self) -> list[str]:
        found = [
            f"{t['symbol']}: {t['problem']}"
            for t in self.exits.trades() if t["status"] == "NEEDS_ATTENTION"
        ]
        # Switched off with a trade still open: say so on every page, so
        # the unwatched take profit is not forgotten.
        if not market_switch.is_on(self.profile.id):
            found += [
                f"{t['symbol']}: India is switched off, so its take profit "
                f"{self._money(t.get('take_profit'))} is not being watched"
                for t in self._watched()
            ]
        return found

    def release_protection(self, identifier: str) -> None:
        if self.ready:
            self.exits.release(identifier)

    # -- reading ------------------------------------------------------------

    def parse_identifier(self, identifier):
        return contracts.parse_identifier(identifier)

    def spot_price(self, symbol):
        if not self.ready:
            _not_ready("read a spot price")
        index_exchange = contracts.INDEX_EXCHANGE.get(self.settings.exchange, "NSE_INDEX")
        return data.spot_price(self.client, symbol, index_exchange, self.profile.session)

    def expirations(self, underlying):
        if not self.ready:
            _not_ready("list expiries")
        return contracts.list_expirations(
            self.client, underlying.strip().upper(), self.settings.exchange, self._today())

    def select_contract(self, underlying, option_type, underlying_price, *,
                        minimum_days, expiry_date_text, strikes_out):
        if not self.ready:
            _not_ready("choose a contract")
        chosen = contracts.select_contract(
            self.client, underlying, option_type, underlying_price,
            exchange=self.settings.exchange, today=self._today(),
            minimum_days=minimum_days, expiry_date_text=expiry_date_text,
            strikes_out=strikes_out, strike_step=self.settings.strike_step,
            strike_choices=self.settings.strike_choices,
        )
        with self._lock:
            self._lots[chosen[0].identifier] = int(chosen[0].multiplier)
        return chosen

    def find_contract(self, underlying, put_call, strike, expiry_date_text):
        if not self.ready:
            _not_ready("resolve a contract")
        identifier = contracts.build_identifier(
            underlying, date.fromisoformat(expiry_date_text), strike, put_call)
        return contracts.describe_contract(
            self.client, identifier, self.settings.exchange, self._today())

    def recent_traded_price(self, identifier):
        if not self.ready:
            _not_ready("read a traded price")
        return data.recent_traded_price(
            self.client, identifier, self.settings.exchange, self.profile.session)

    def minute_bars(self, identifier, *, begin, end):
        if not self.ready:
            _not_ready("read minute bars")
        return data.minute_bars(self.client, identifier, self.settings.exchange, begin, end)

    def positions(self):
        if not self.ready:
            _not_ready("read positions")
        held = []
        for row in read_rows(self.client, "positionbook", "The position book"):
            if str(row.get("exchange") or "").upper() != self.settings.exchange:
                continue
            position = to_option_position(row, self._lot(str(row.get("symbol") or "")), self._today())
            if position is not None:
                held.append(position)
        return sorted(held, key=lambda p: (p.days_to_expiry, p.identifier))

    def orders(self, limit):
        if not self.ready:
            _not_ready("read orders")
        rows = [r for r in read_rows(self.client, "orderbook", "The order book")
                if str(r.get("exchange") or "").upper() == self.settings.exchange]
        fills = fills_by_order(read_rows(self.client, "tradebook", "The trade book"))
        links = self.exits.links()
        orders = [to_broker_order(r, fills, self._lot(str(r.get("symbol") or "")), links)
                  for r in rows]
        orders.sort(key=lambda o: o.order_time or 0, reverse=True)
        return orders[:limit]

    def open_orders(self):
        return [o for o in self.orders(500) if o.status in WORKING]

    def filled_orders(self, start, end):
        # Angel's books hold today only; the market-day filter does the rest.
        return [o for o in self.orders(500) if o.filled > 0]

    def order(self, order_id):
        return next((o for o in self.orders(500) if o.id == str(order_id)), None)

    def attached_legs(self, order_id):
        legs = []
        for o in self.orders(500):
            if o.parent_id != str(order_id):
                continue
            legs.append({
                "source": "child order", "id": o.id,
                "order_type": "STP" if "STP" in o.order_type else "LMT",
                "action": o.action, "quantity": o.quantity,
                "limit_price": o.limit_price, "aux_price": o.aux_price,
                "time_in_force": o.time_in_force, "status": o.status,
                "avg_fill_price": o.avg_fill_price, "filled": o.filled,
            })
        return legs

    # -- sending ------------------------------------------------------------
    #
    # Every order goes through submit.py, where its guard runs immediately
    # before placeorder. Cancelling spends nothing and needs no guard.

    def cancel_order(self, order_id):
        if not self.ready:
            _not_ready("cancel an order")
        unwrap(self.client.cancelorder(order_id=str(order_id), strategy=STRATEGY),
               f"Cancelling order {order_id}")

    def _outcome(self, order_id, requested_lots, lot) -> FillOutcome:
        """One read of what an order did -- no sleeping; the page follows it."""
        try:
            rows = read_rows(self.client, "orderbook", "The order book")
            fills = fills_by_order(read_rows(self.client, "tradebook", "The trade book"))
        except Exception:  # noqa: BLE001 -- the order is placed; its status can wait
            rows, fills = [], {}
        row = next((r for r in rows if str(r.get("orderid")) == str(order_id)), {})
        status = STATUS.get(str(row.get("order_status") or "").strip().lower(), "SUBMITTED")
        done = fills.get(str(order_id))
        filled_lots = int(round((done.units if done else 0) / lot))
        average = done.average if done else None
        return FillOutcome(
            order_id=order_id, status=status, requested_quantity=requested_lots,
            filled_quantity=filled_lots, average_fill_price=average,
            actual_cash=calculate_actual_cash(average, filled_lots, lot),
            outcome=classify_fill(requested_lots, filled_lots), poll_attempts=1,
            reached_terminal_status=is_terminal_status(status),
            reason=str(row.get("rejection_reason") or ""),
        )

    def buy_option_with_bracket(self, settings, contract, quote, quantity, *,
                                take_profit_price, stop_loss_price,
                                leg_time_in_force="DAY", input_function=input,
                                on_submitted=None, **_ignored):
        if not self.ready:
            _not_ready("place an order")

        estimate = estimate_cost(
            contract=contract, action="BUY", quantity=quantity,
            bid=quote.bid, ask=quote.ask, limit_price=quote.limit_price,
        )
        validate_bracket_prices(estimate.price_used, take_profit_price, stop_loss_price)
        legs = BracketLegs(
            take_profit_price=take_profit_price, stop_loss_price=stop_loss_price,
            leg_time_in_force=leg_time_in_force,
        )
        if not _confirmed(estimate, input_function):
            raise OrderSubmissionError("Cash amount not confirmed. Nothing was submitted.")

        lot = int(contract.multiplier)
        units = int(round(estimate.quantity * lot))
        order_id, mode = place_entry(
            self.client, settings, symbol=contract.identifier, units=units,
            limit_price=quote.limit_price,
        )

        # Watched from this instant: the exits follow the fill, however soon.
        self.exits.watch_entry(
            order_id=order_id, symbol=contract.identifier, units=units, lot_size=lot,
            limit_price=quote.limit_price, take_profit=take_profit_price,
            stop_loss=stop_loss_price, tick=settings.option_tick_size, mode=mode,
        )
        if on_submitted is not None:
            on_submitted(order_id, estimate)
        return self._outcome(order_id, estimate.quantity, lot), estimate, legs

    def sell_option(self, settings, contract, quote, quantity, *,
                    input_function=input, on_submitted=None, **_ignored):
        if not self.ready:
            _not_ready("place an order")

        estimate = estimate_cost(
            contract=contract, action="SELL", quantity=quantity,
            bid=quote.bid, ask=quote.ask, limit_price=quote.limit_price,
        )
        if not _confirmed(estimate, input_function):
            raise OrderSubmissionError("Cash amount not confirmed. Nothing was submitted.")

        lot = int(contract.multiplier)
        # The position was read from OpenAlgo's book in the mode it is in NOW,
        # so selling in that same mode reaches the same place.
        mode = resolve_mode(self.client, settings.allow_live)
        order_id = place_exit(
            self.client, settings, symbol=contract.identifier,
            units=int(round(quantity * lot)), opened_in=mode,
            price_type="LIMIT", price=quote.limit_price,
        )
        if on_submitted is not None:
            on_submitted(order_id, estimate)
        return self._outcome(order_id, quantity, lot), estimate
