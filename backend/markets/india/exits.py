"""Take profit and stop loss for India: one exit on the book at a time, never two.

WHY NOT BOTH EXITS AT THE BROKER. Angel has no native one-cancels-other. Two
exit orders resting together are two independent SELLs of the same lots, and
in a fast market both can fill -- which sells the position twice and leaves a
SHORT option, with no limit to what it can lose. The earlier backend (Angel
OCO triggers plus a sweep cancelling "the other one") was that design with a
race in it, and it broke on exactly those days.

SO THIS KEEPS EXACTLY ONE EXIT LIVE:

    the STOP LOSS rests at the exchange as a stop-limit order, placed the
        moment the entry fills. It protects the position even if this
        machine is switched off.

    the TAKE PROFIT is watched here. When the price reaches it, the stop is
        cancelled FIRST, the cancel is confirmed, and only then is the sell
        sent. If the stop fills in the meantime, there is nothing to sell.

    whichever fires, there is no "other one" left to cancel.

AN EXIT THAT CANNOT FILL. A stop fired into a gap posts a limit above a market
that has already fallen through it, and never fills. An exit left unfilled for
STUCK_EXIT_SECONDS is cancelled and the rest is sold at MARKET.

A POSITION THAT LEAVES BY ANOTHER ROUTE. Closed from the page, or from the
Angel app: its stop is cancelled -- a stop left behind on a position no longer
held would, if triggered, SELL SHORT -- and it stops being watched.

STATE ON DISK. state/in/exits.json holds every trade being managed, so a
restart carries on where it stopped. Nothing is inferred from the broker's
books that this service did not write down itself.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.core import paths
from backend.core.safety import write_order_record
from backend.services.order import snap_down

from .books import STATUS, WORKING, fills_by_order, read_rows
from .data import read_quote
from .openalgo import STRATEGY
from .submit import place_exit

FILENAME = "exits.json"

#: Waiting for the entry to fill.
WAITING = "WAITING_FOR_FILL"
#: Filled, and the stop loss is resting at the exchange.
PROTECTED = "PROTECTED"
#: The take profit was reached; the stop is being cancelled before selling.
TAKING_PROFIT = "TAKING_PROFIT"
#: An exit SELL is on the book: the take profit, or a stop that fired.
EXITING = "EXITING"
#: Finished: it exited, or never filled.
DONE = "DONE"
#: Closed by some other route; no longer watched.
RELEASED = "RELEASED"
#: Something failed that a human must look at. The page says what.
NEEDS_ATTENTION = "NEEDS_ATTENTION"

ACTIVE = (WAITING, PROTECTED, TAKING_PROFIT, EXITING)

#: The limit of an exit may concede at most this share of its price, so a
#: cheap option does not give most of its value away.
SLIPPAGE_MAX_FRACTION = 0.10

#: Seconds to wait for a requested cancel before asking again.
CANCEL_RETRY_SECONDS = 10

#: Finished trades are kept this long, so today's history can still say
#: which exit belonged to which entry.
KEEP_FINISHED_DAYS = 3

#: A position must be missing from the broker's position book this many
#: ticks in a row, and at least NOT_HELD_GRACE_SECONDS after its stop was
#: placed, before it is taken as closed elsewhere. The position book can lag
#: the order book by a moment after a fill; reading one empty answer as
#: "closed" would cancel the stop of a position that is very much held.
NOT_HELD_TICKS = 3
NOT_HELD_GRACE_SECONDS = 15


def _now() -> float:
    return time.time()


class ExitManager:
    """Watches India's open trades and runs their exits. Thread-safe."""

    def __init__(self, market, *, poll_seconds: float, stuck_seconds: float,
                 slippage_ticks: int, clock=_now):
        self.market = market
        self.poll_seconds = poll_seconds
        self.stuck_seconds = stuck_seconds
        self.slippage_ticks = slippage_ticks
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trades: dict[str, dict] = self._load()

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------

    @staticmethod
    def path() -> Path:
        return paths.state_file("IN", FILENAME)

    def _load(self) -> dict[str, dict]:
        try:
            stored = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(stored, dict):
            return {}
        cutoff = self.clock() - KEEP_FINISHED_DAYS * 86400
        return {
            key: trade for key, trade in stored.items()
            if isinstance(trade, dict)
            and (trade.get("status") in ACTIVE or trade.get("updated", 0) >= cutoff)
        }

    def _save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".exits-",
            suffix=".tmp", delete=False,
        )
        try:
            with handle:
                json.dump(self._trades, handle, indent=2, sort_keys=True)
            Path(handle.name).replace(path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # What the rest of the service asks
    # ------------------------------------------------------------------

    def watch_entry(self, *, order_id: str, symbol: str, units: int, lot_size: int,
                    limit_price: float, take_profit: float, stop_loss: float,
                    tick: float, mode: str) -> None:
        """Start managing a BUY that was just sent."""
        now = self.clock()
        with self._lock:
            self._trades[order_id] = {
                "entry_order_id": order_id, "symbol": symbol, "units": int(units),
                "lot_size": int(lot_size), "entry_limit": limit_price,
                "take_profit": take_profit, "stop_loss": stop_loss, "tick": tick,
                "mode": mode, "status": WAITING, "filled_units": 0,
                "stop_order_id": None, "exit_order_id": None, "exit_kind": None,
                "exit_units": 0, "market_used": False, "cancel_requested_at": None,
                "open_since": None, "outcome": None, "problem": None,
                "placed_ms": int(now * 1000), "orders": {}, "created": now,
                "updated": now,
            }
            self._save()

    def release(self, symbol: str) -> list[str]:
        """Stop managing every trade on a contract, because it is being closed
        by another route. Returns the stop and exit orders still working, for
        the caller to cancel before it sells."""
        wanted = symbol.strip().upper()
        orders = []
        with self._lock:
            for trade in self._trades.values():
                if trade["symbol"].upper() == wanted and trade["status"] in ACTIVE:
                    trade["status"] = RELEASED
                    trade["outcome"] = "closed by another route"
                    trade["updated"] = self.clock()
                    orders += [o for o in (trade["stop_order_id"], trade["exit_order_id"]) if o]
            self._save()
        return orders

    def links(self) -> dict[str, dict]:
        """For each order this service placed: its entry, role and limit.

        What lets the history show "stop loss" and "take profit" for exits that,
        at the broker, are plain standalone SELLs.
        """
        with self._lock:
            out = {}
            for trade in self._trades.values():
                out[trade["entry_order_id"]] = {
                    "limit_price": trade["entry_limit"], "placed_ms": trade["placed_ms"],
                }
                for order_id, known in trade["orders"].items():
                    out[order_id] = {**known, "parent_id": trade["entry_order_id"]}
            return out

    def trades(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._trades.values()]

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="india-exits", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        """Tick until stopped. A failed tick never ends the loop: a broker hiccup
        must not silently switch exits off for the rest of the session."""
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 -- recorded, loop continues
                self._audit({"action": "exit_tick_failed",
                             "problem": f"{type(error).__name__}: {error}"})
            self._stop.wait(self.poll_seconds)

    def tick(self) -> None:
        """Read the books once, then move every active trade one step.

        Nothing at all while India is switched off on the Settings page:
        OpenAlgo is not asked, and every trade resumes where it was when
        India is switched on again.
        """
        if not getattr(self.market, "ready", True):
            return
        with self._lock:
            active = [t for t in self._trades.values() if t["status"] in ACTIVE]
        if not active:
            return

        client = self.market.client
        orders = {str(r.get("orderid")): r for r in read_rows(client, "orderbook", "The order book")}
        fills = fills_by_order(read_rows(client, "tradebook", "The trade book"))
        held: dict[str, float] = {}
        for row in read_rows(client, "positionbook", "The position book"):
            symbol = str(row.get("symbol") or "").upper()
            held[symbol] = held.get(symbol, 0) + float(row.get("quantity") or 0)

        prices = {}
        for symbol in {t["symbol"] for t in active if t["status"] in (WAITING, PROTECTED, TAKING_PROFIT)}:
            try:
                prices[symbol] = float(read_quote(client, symbol, self.market.settings.exchange)["ltp"])
            except Exception:  # noqa: BLE001 -- no price: the step waits
                prices[symbol] = None

        for trade in active:
            with self._lock:
                if trade["status"] not in ACTIVE:
                    continue  # released while the books were being read
                before = json.dumps(trade, sort_keys=True)
                try:
                    self._step(trade, orders, fills, held, prices.get(trade["symbol"]))
                except Exception as error:  # noqa: BLE001 -- one trade must not stop the rest
                    self._attention(
                        trade, f"An exit step failed ({type(error).__name__}: {error}). "
                        "This position may have NO STOP -- check it and close it "
                        "by hand if needed.")
                if json.dumps(trade, sort_keys=True) != before:
                    trade["updated"] = self.clock()
                    self._save()

    # ------------------------------------------------------------------
    # One step of one trade
    # ------------------------------------------------------------------

    def _status(self, orders, order_id) -> str | None:
        row = orders.get(str(order_id)) if order_id else None
        if row is None:
            return None
        text = str(row.get("order_status") or "").strip().lower()
        return STATUS.get(text, text.upper())

    def _filled(self, fills, order_id) -> int:
        entry = fills.get(str(order_id)) if order_id else None
        return int(entry.units) if entry else 0

    def _step(self, trade, orders, fills, held, ltp) -> None:
        status = trade["status"]

        if status == WAITING:
            entry_status = self._status(orders, trade["entry_order_id"])
            filled = self._filled(fills, trade["entry_order_id"])
            if entry_status in ("FILLED", "CANCELLED", "REJECTED"):
                if filled <= 0:
                    trade["status"] = DONE
                    trade["outcome"] = f"entry {entry_status.lower()}, nothing bought"
                    return
                trade["filled_units"] = filled
                self._protect(trade, ltp)
            return

        # From here the position exists. If the broker no longer holds it,
        # something else closed it: nothing may be left that could sell again.
        stop_status = self._status(orders, trade["stop_order_id"])
        if status in (PROTECTED, TAKING_PROFIT) and stop_status != "FILLED":
            if held.get(trade["symbol"].upper(), 0) > 0:
                trade["not_held_ticks"] = 0
            else:
                trade["not_held_ticks"] = trade.get("not_held_ticks", 0) + 1
                settled = self.clock() - (trade.get("protected_at") or 0) >= NOT_HELD_GRACE_SECONDS
                if trade["not_held_ticks"] >= NOT_HELD_TICKS and settled:
                    if stop_status in WORKING:
                        self._cancel(trade["stop_order_id"])
                    trade["status"] = RELEASED
                    trade["outcome"] = "no longer held -- closed outside this service"
                    return

        if status == PROTECTED:
            self._step_protected(trade, orders, fills, ltp)
        elif status == TAKING_PROFIT:
            self._step_taking_profit(trade, orders, fills, ltp)
        elif status == EXITING:
            self._step_exiting(trade, orders, fills)

    def _step_protected(self, trade, orders, fills, ltp) -> None:
        stop_id = trade["stop_order_id"]
        stop_status = self._status(orders, stop_id)

        if stop_status == "FILLED":
            self._finish(trade, "STOPPED_OUT")
            return
        if stop_status == "REJECTED":
            self._attention(
                trade, "The stop loss was REJECTED by the exchange -- this position "
                "has NO STOP. Close it or place a stop by hand.")
            return
        if stop_status == "CANCELLED":
            trade["status"] = RELEASED
            trade["outcome"] = "stop cancelled outside this service; no longer watched"
            return

        if stop_status == "SUBMITTED":
            # Triggered, and its limit is resting unfilled.
            if trade["open_since"] is None:
                trade["open_since"] = self.clock()
            elif self.clock() - trade["open_since"] >= self.stuck_seconds:
                self._escalate(trade, stop_id, "STOP_LOSS", fills)
            return

        if ltp is not None and ltp >= trade["take_profit"]:
            # Cancel FIRST. Nothing is sold until the stop is confirmed gone.
            self._cancel(stop_id)
            trade["status"] = TAKING_PROFIT
            trade["cancel_requested_at"] = self.clock()
            trade["target_seen_at"] = ltp

    def _step_taking_profit(self, trade, orders, fills, ltp) -> None:
        stop_id = trade["stop_order_id"]
        stop_status = self._status(orders, stop_id)

        if stop_status == "FILLED":
            self._finish(trade, "STOPPED_OUT")  # it filled before the cancel landed
            return
        if stop_status in ("CANCELLED", "REJECTED"):
            remaining = trade["filled_units"] - self._filled(fills, stop_id)
            if remaining <= 0:
                self._finish(trade, "STOPPED_OUT")
                return
            price = ltp if ltp is not None else trade["take_profit"]
            self._send_exit(trade, "TAKE_PROFIT", remaining, "LIMIT", self._limit_below(trade, price))
            return
        if self.clock() - (trade["cancel_requested_at"] or 0) >= CANCEL_RETRY_SECONDS:
            self._cancel(stop_id)
            trade["cancel_requested_at"] = self.clock()

    def _step_exiting(self, trade, orders, fills) -> None:
        exit_id = trade["exit_order_id"]
        exit_status = self._status(orders, exit_id)
        filled = self._filled(fills, exit_id)
        remaining = trade["exit_units"] - filled

        if exit_status == "FILLED" or remaining <= 0:
            self._finish(trade, trade["exit_kind"])
            return
        if exit_status in ("CANCELLED", "REJECTED"):
            # Cancelled by the stuck-exit rule, or refused: sell what is left
            # at market, once.
            if trade["market_used"]:
                self._attention(
                    trade, f"The market exit was {exit_status.lower()} -- "
                    f"{remaining} units may still be held with NO STOP. "
                    "Close it by hand.")
                return
            trade["market_used"] = True
            self._send_exit(trade, trade["exit_kind"], remaining, "MARKET", None)
            return
        if exit_status in WORKING:
            if trade["open_since"] is None:
                trade["open_since"] = self.clock()
            elif (self.clock() - trade["open_since"] >= self.stuck_seconds
                  and not trade["market_used"] and trade["cancel_requested_at"] is None):
                self._cancel(exit_id)
                trade["cancel_requested_at"] = self.clock()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _protect(self, trade, ltp) -> None:
        """The entry filled: rest the stop at the exchange, or sell now if the
        price is already through it."""
        units = trade["filled_units"]
        stop = trade["stop_loss"]
        if ltp is not None and ltp <= stop:
            self._send_exit(trade, "STOP_LOSS", units, "LIMIT", self._limit_below(trade, ltp))
            return

        order_id = place_exit(
            self.market.client, self.market.settings, symbol=trade["symbol"],
            units=units, opened_in=trade["mode"], price_type="SL",
            price=self._limit_below(trade, stop), trigger_price=stop,
        )
        trade["stop_order_id"] = order_id
        trade["orders"][order_id] = {"role": "STOP_LOSS", "limit_price": self._limit_below(trade, stop),
                                     "trigger_price": stop, "placed_ms": int(self.clock() * 1000)}
        trade["status"] = PROTECTED
        trade["protected_at"] = self.clock()
        trade["not_held_ticks"] = 0
        trade["open_since"] = None
        self._audit({"action": "stop_loss_placed", "entry_order_id": trade["entry_order_id"],
                     "order_id": order_id, "symbol": trade["symbol"], "units": units,
                     "trigger_price": stop, "limit_price": self._limit_below(trade, stop)})

    def _send_exit(self, trade, kind: str, units: int, price_type: str, price) -> None:
        order_id = place_exit(
            self.market.client, self.market.settings, symbol=trade["symbol"],
            units=units, opened_in=trade["mode"], price_type=price_type, price=price,
        )
        trade["exit_order_id"] = order_id
        trade["exit_kind"] = kind
        trade["exit_units"] = units
        trade["orders"][order_id] = {"role": kind, "limit_price": price,
                                     "placed_ms": int(self.clock() * 1000)}
        trade["status"] = EXITING
        trade["open_since"] = None
        trade["cancel_requested_at"] = None
        self._audit({"action": f"{kind.lower()}_exit_sent", "entry_order_id": trade["entry_order_id"],
                     "order_id": order_id, "symbol": trade["symbol"], "units": units,
                     "price_type": price_type, "limit_price": price})

    def _escalate(self, trade, order_id, kind: str, fills) -> None:
        """A triggered stop that cannot fill: cancel it, then sell at market."""
        self._cancel(order_id)
        trade["exit_order_id"] = order_id
        trade["exit_kind"] = kind
        trade["exit_units"] = trade["filled_units"]
        trade["status"] = EXITING
        trade["cancel_requested_at"] = self.clock()
        self._audit({"action": "stuck_stop_cancelled", "order_id": order_id,
                     "entry_order_id": trade["entry_order_id"], "symbol": trade["symbol"]})

    def _cancel(self, order_id) -> None:
        """Ask OpenAlgo to cancel. Its effect is read from the books next tick."""
        if not order_id:
            return
        try:
            self.market.client.cancelorder(order_id=order_id, strategy=STRATEGY)
        except Exception as error:  # noqa: BLE001 -- retried on a later tick
            self._audit({"action": "cancel_failed", "order_id": order_id,
                         "problem": f"{type(error).__name__}: {error}"})

    def _finish(self, trade, outcome: str) -> None:
        trade["status"] = DONE
        trade["outcome"] = outcome
        self._audit({"action": "trade_finished", "entry_order_id": trade["entry_order_id"],
                     "symbol": trade["symbol"], "outcome": outcome})

    def _attention(self, trade, problem: str) -> None:
        trade["status"] = NEEDS_ATTENTION
        trade["problem"] = problem
        self._audit({"action": "needs_attention", "entry_order_id": trade["entry_order_id"],
                     "symbol": trade["symbol"], "problem": problem})

    def _limit_below(self, trade, price: float) -> float:
        """A SELL limit a little under `price`, so a moving market still fills it."""
        tick = trade["tick"]
        allowance = min(self.slippage_ticks * tick, price * SLIPPAGE_MAX_FRACTION)
        return snap_down(max(price - allowance, tick), tick)

    def _audit(self, record: dict) -> None:
        try:
            write_order_record({
                "market": "IN", "source": "india-exits",
                "at": datetime.now(timezone.utc).isoformat(), **record,
            })
        except Exception:  # noqa: BLE001 -- the audit must never stop an exit
            pass
