"""US options through Tiger Brokers, as a `Market`.

Everything the routes used to do with a Tiger client, they now ask of this
object. It holds the two clients, reaches the broker through the same library
functions as before, and hands back neutral shapes -- most importantly
`BrokerOrder` instead of Tiger's own Order, so nothing outside this folder has
to know how Tiger spells a contract or a status.

THE CLIENTS ARE BUILT LAZILY, ONCE. Each one reads and parses the private
key, and the SDK recommends one QuoteClient reused rather than many. The quote
client is built with grab_permission=False, as the CLI does: claiming market
data device access would take primary-device status away from whatever held
it, such as the Tiger app on a phone.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tigeropen.common.consts import SecurityType

from backend.core.broker import (
    OPEN_ORDERS_LIMITER,
    ORDERS_LIMITER,
    build_quote_client,
    build_trade_client,
)
from backend.services.contract import (
    find_option_contract,
    parse_identifier,
    select_contract,
)
from backend.services.market import (
    fetch_recent_traded_price,
    fetch_spot_price,
    list_expirations,
)
from backend.services.market.bars import fetch_minute_bars
from backend.services.order import (
    buy_option_with_bracket,
    cancel_order,
    get_attached_legs,
    get_order_status,
    normalise_status,
    sell_option,
)
from backend.services.position import list_option_positions

from ..base import BrokerOrder, Market, MarketProfile

PROFILE = MarketProfile(
    id="US",
    name="US options (Tiger)",
    timezone=ZoneInfo("America/New_York"),
    currency="USD",
)


def to_broker_order(raw) -> BrokerOrder:
    """Turn one of Tiger's Order objects into the neutral shape.

    Tiger renders the contract as "AAPL  260918C00360000/OPT/USD"; the part
    before the first slash is the identifier positions use, inner spacing and
    all. The underlying is its first word.

    Args:
        raw: A tigeropen Order.

    Returns:
        The same order as a BrokerOrder.
    """
    contract = getattr(raw, "contract", None)
    identifier = str(contract or "").split("/")[0]
    order_type = getattr(raw, "order_type", None)
    time_in_force = getattr(raw, "time_in_force", None)
    multiplier = getattr(contract, "multiplier", None)

    return BrokerOrder(
        id=getattr(raw, "id", None),
        parent_id=getattr(raw, "parent_id", None),
        identifier=identifier,
        underlying=identifier.strip().split(" ")[0].strip().upper(),
        action=str(getattr(raw, "action", "") or "").upper(),
        status=normalise_status(getattr(raw, "status", None)),
        order_type=str(order_type) if order_type else "",
        quantity=float(getattr(raw, "quantity", 0) or 0),
        filled=float(getattr(raw, "filled", 0) or 0),
        avg_fill_price=getattr(raw, "avg_fill_price", None),
        limit_price=getattr(raw, "limit_price", None),
        aux_price=getattr(raw, "aux_price", None),
        time_in_force=str(time_in_force) if time_in_force else None,
        order_time=getattr(raw, "order_time", None),
        trade_time=getattr(raw, "trade_time", None),
        reason=str(getattr(raw, "reason", "") or "") or None,
        filled_cash_amount=getattr(raw, "filled_cash_amount", None),
        multiplier=float(multiplier) if multiplier else None,
    )


class UsMarket(Market):
    """US options through Tiger Brokers."""

    profile = PROFILE

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._quote_client = None
        self._trade_client = None

    # -- the clients --------------------------------------------------------

    @property
    def quote_client(self):
        """The shared tigeropen QuoteClient, built on first use."""
        with self._lock:
            if self._quote_client is None:
                self._quote_client = build_quote_client(
                    self.settings, grab_permission=False
                )
            return self._quote_client

    @property
    def trade_client(self):
        """The shared tigeropen TradeClient, built on first use."""
        with self._lock:
            if self._trade_client is None:
                self._trade_client = build_trade_client(self.settings)
            return self._trade_client

    # -- reading ------------------------------------------------------------

    def parse_identifier(self, identifier):
        # The 21-character OCC code, e.g. "AAPL  260918C00360000".
        return parse_identifier(identifier)

    def spot_price(self, symbol):
        # From Yahoo, not Tiger: this account holds no usStockQuote
        # entitlement, so Tiger's own price is about 15 minutes stale.
        return fetch_spot_price(symbol)

    def expirations(self, underlying):
        return list_expirations(self.quote_client, underlying)

    def select_contract(
        self, underlying, option_type, underlying_price, *,
        minimum_days, expiry_date_text, strikes_out,
    ):
        return select_contract(
            self.quote_client,
            self.trade_client,
            underlying,
            option_type,
            underlying_price,
            minimum_days=minimum_days,
            expiry_date_text=expiry_date_text,
            strikes_out=strikes_out,
        )

    def find_contract(self, underlying, put_call, strike, expiry_date_text):
        return find_option_contract(
            self.quote_client,
            self.trade_client,
            underlying,
            put_call,
            strike,
            expiry_date_text,
        )

    def recent_traded_price(self, identifier):
        return fetch_recent_traded_price(self.quote_client, identifier)

    def minute_bars(self, identifier, *, begin, end):
        return fetch_minute_bars(self.quote_client, identifier, begin=begin, end=end)

    def positions(self):
        return list_option_positions(self.trade_client)

    def orders(self, limit):
        ORDERS_LIMITER.wait()
        raw = self.trade_client.get_orders(
            account=self.settings.account, sec_type=SecurityType.OPT, limit=limit
        )
        return [to_broker_order(order) for order in raw or []]

    def open_orders(self):
        OPEN_ORDERS_LIMITER.wait()
        raw = self.trade_client.get_open_orders(
            account=self.settings.account, sec_type=SecurityType.OPT
        )
        return [to_broker_order(order) for order in raw or []]

    def filled_orders(self, start: date, end: date):
        ORDERS_LIMITER.wait()
        raw = self.trade_client.get_filled_orders(
            account=self.settings.account,
            sec_type=SecurityType.OPT,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        return [to_broker_order(order) for order in raw or []]

    def order(self, order_id):
        raw = get_order_status(self.trade_client, order_id)
        return to_broker_order(raw) if raw is not None else None

    def attached_legs(self, order_id):
        return get_attached_legs(self.trade_client, order_id)

    # -- sending ------------------------------------------------------------
    #
    # Each of these ends in backend/services/order/submit.py, where
    # assert_order_allowed runs immediately before place_order.

    def cancel_order(self, order_id):
        cancel_order(self.trade_client, order_id)

    def buy_option_with_bracket(self, settings, contract, quote, quantity, **kwargs):
        return buy_option_with_bracket(
            self.trade_client, settings, contract, quote, quantity, **kwargs
        )

    def sell_option(self, settings, contract, quote, quantity, **kwargs):
        return sell_option(self.trade_client, settings, contract, quote, quantity, **kwargs)
