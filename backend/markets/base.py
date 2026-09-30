"""What every market must provide, and the neutral shapes it answers in.

WHY AN INTERFACE. The routes used to hold a Tiger client and read Tiger's own
objects field by field. A second broker would have meant a second copy of
every route. Now a route asks a `Market` -- "what is held?", "what orders were
placed?" -- and each market answers in the SAME shapes, whatever its broker
calls things.

WHAT IS NEUTRAL. `BrokerOrder` below, and the dataclasses the US market
already returned: OptionContractInfo, OptionPosition, OptionExpiry,
RecentTrade, SpotPrice, Bar, FillOutcome. A new market builds those; it does
not invent its own.

THE SAFETY RULE STAYS WHERE IT WAS. The two methods that can spend money,
`buy_option_with_bracket` and `sell_option`, must run `assert_order_allowed`
immediately before the broker call, inside the market's own submission file.
A market that cannot tell paper from live must refuse to be armed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.core.time_brackets import Session


class UnknownMarket(Exception):
    """A request named a market this service does not trade."""


class MarketNotReady(Exception):
    """The market is configured, but cannot do this yet. Nothing was sent."""


@dataclass(frozen=True)
class MarketProfile:
    """The facts about a market that are not about its broker."""

    id: str               # "US" -- what a request names it by
    name: str             # for people, e.g. "US options (Tiger)"
    currency: str         # "USD"
    currency_symbol: str  # "$" -- for messages and the page
    session: Session      # when it trades, on its own clock

    #: Units one contract controls when an order does not say. A US option
    #: is 100 shares. Where a broker counts quantity in units already -- as
    #: Indian F&O does, in multiples of the lot -- this is 1.
    contract_multiplier: float

    @property
    def timezone(self) -> ZoneInfo:
        """The exchange's clock. A trading day is ITS day."""
        return self.session.timezone

    def today(self) -> date:
        """The trading day it is now, on the exchange's own clock."""
        return datetime.now(self.timezone).date()

    def money(self, amount: float) -> str:
        """An amount in this market's currency, e.g. "$800.00" or "₹10,000.00"."""
        return f"{self.currency_symbol}{amount:,.2f}"


@dataclass(frozen=True)
class BrokerOrder:
    """One order, as any broker reports it.

    The field names are the ones the order history, the export and the
    one-trade-per-symbol guard already read, so none of them needed to change
    when orders stopped arriving as Tiger objects.
    """

    id: int | str | None
    parent_id: int | str | None   # set on a bracket leg: the entry it exits
    identifier: str               # the contract, as positions name it
    underlying: str               # "NVDA"
    action: str                   # BUY or SELL
    status: str                   # an upper-case name: FILLED, CANCELLED ...
    order_type: str               # LMT, STP ...
    quantity: float
    filled: float
    avg_fill_price: float | None
    limit_price: float | None     # a take-profit's price lives here ...
    aux_price: float | None       # ... and a stop-loss's lives here
    time_in_force: str | None
    order_time: int | None        # epoch milliseconds: accepted
    trade_time: int | None        # epoch milliseconds: filled
    reason: str | None            # the broker's own words, when it gave any
    filled_cash_amount: float | None
    multiplier: float | None      # units per contract, when the broker said


class Market(ABC):
    """A market this service can trade, behind one set of endpoints."""

    profile: MarketProfile

    #: The market's own trading settings -- a TradingSettings from its file.
    settings: object

    #: False while the market is configured but cannot trade yet. Its
    #: settings, symbols and switches still work; the broker calls raise
    #: MarketNotReady.
    ready: bool = True

    # -- reading. Nothing below can place an order. -------------------------

    @abstractmethod
    def parse_identifier(self, identifier: str) -> tuple[str, str, str, float]:
        """Read a contract name: (underlying, expiry YYYY-MM-DD, CALL/PUT, strike).

        Every broker writes contract names its own way, so only the market can
        read one. Raises ValueError for a name it does not recognise.
        """

    @abstractmethod
    def spot_price(self, symbol: str):
        """The underlying's current price, or None when it cannot be read."""

    @abstractmethod
    def expirations(self, underlying: str) -> list:
        """Every listed expiry for an underlying, as OptionExpiry."""

    @abstractmethod
    def select_contract(
        self, underlying: str, option_type: str, underlying_price: float, *,
        minimum_days: int, expiry_date_text: str | None, strikes_out: int,
    ):
        """Choose the contract to trade: (contract, why this expiry, why this strike)."""

    @abstractmethod
    def find_contract(
        self, underlying: str, put_call: str, strike: float, expiry_date_text: str
    ):
        """Resolve one exact contract, as OptionContractInfo."""

    @abstractmethod
    def recent_traded_price(self, identifier: str):
        """The contract's last traded price, as RecentTrade, or None."""

    @abstractmethod
    def minute_bars(self, identifier: str, *, begin: datetime, end: datetime) -> list:
        """One-minute bars for a contract over a window, as Bar."""

    @abstractmethod
    def positions(self) -> list:
        """What is held, as OptionPosition. Always a live read."""

    @abstractmethod
    def orders(self, limit: int) -> list[BrokerOrder]:
        """The most recent orders, bracket legs included."""

    @abstractmethod
    def open_orders(self) -> list[BrokerOrder]:
        """Orders still working on the broker's book."""

    @abstractmethod
    def filled_orders(self, start: date, end: date) -> list[BrokerOrder]:
        """Orders that filled between two days, for realised P&L."""

    @abstractmethod
    def order(self, order_id) -> BrokerOrder | None:
        """One order by the broker's id, or None when it has no such order."""

    @abstractmethod
    def attached_legs(self, order_id) -> list[dict]:
        """The take-profit and stop-loss legs attached to an entry order."""

    # -- background work. Optional: most markets have none. ----------------

    def start(self) -> None:
        """Begin any background work, when the server starts."""

    def stop(self) -> None:
        """End it, when the server stops."""

    def planned_exits(self, order_id) -> dict | None:
        """The take-profit and stop-loss levels an entry was given, when this
        market runs its exits itself rather than as orders at the broker.

        Returns {"take_profit": ..., "stop_loss": ...}, or None. A market whose
        broker holds both legs as orders -- Tiger -- has nothing to add.
        """
        return None

    def alerts(self) -> list[str]:
        """Problems a human must act on now -- a position left without a stop.

        Shown in red on the page. A market that manages nothing itself has none.
        """
        return []

    def release_protection(self, identifier: str) -> None:
        """Stop managing a position's exits, because it is being closed by hand.

        Called before the close route cancels the working exits and sells. A
        market whose broker holds the exits itself -- Tiger attaches them to
        the order -- has nothing to do here.
        """

    # -- sending. Each runs assert_order_allowed right before the broker. ---

    @abstractmethod
    def cancel_order(self, order_id) -> None:
        """Cancel one working order."""

    @abstractmethod
    def buy_option_with_bracket(self, settings, contract, quote, quantity, **kwargs):
        """Place a BUY with its take-profit and stop-loss attached."""

    @abstractmethod
    def sell_option(self, settings, contract, quote, quantity, **kwargs):
        """Place a SELL, to close a held position."""
