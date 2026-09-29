"""NIFTY options through Angel One via OpenAlgo, as a `Market`.

NOT CONNECTED YET. This is the market's place in the service: its profile,
its settings from config/india.env, and its own switches on the page. Every
call that would reach the broker raises MarketNotReady until the OpenAlgo
connection is built -- so nothing about India can place an order, or pretend
to have read one, before then.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo
from datetime import time

from backend.core.time_brackets import Session

from ..base import Market, MarketNotReady, MarketProfile

#: The NSE F&O session, in IST.
NSE_SESSION = Session(ZoneInfo("Asia/Kolkata"), time(9, 15), time(15, 30))

PROFILE = MarketProfile(
    id="IN",
    name="India options (Angel One)",
    currency="INR",
    currency_symbol="\u20b9",
    session=NSE_SESSION,
    # Indian F&O quantities are already in units -- 1 lot of NIFTY is 65.
    contract_multiplier=1.0,
)


def _not_ready(what: str):
    raise MarketNotReady(
        f"India cannot {what} yet: the OpenAlgo connection is not built. "
        "Its settings, symbols and switches work; trading does not."
    )


class IndiaMarket(Market):
    """NIFTY options through Angel One via OpenAlgo. Settings only, so far."""

    profile = PROFILE
    ready = False

    def __init__(self, settings):
        self.settings = settings

    def parse_identifier(self, identifier):
        _not_ready("read a contract name")

    def spot_price(self, symbol):
        _not_ready("read a spot price")

    def expirations(self, underlying):
        _not_ready("list expiries")

    def select_contract(self, underlying, option_type, underlying_price, *,
                        minimum_days, expiry_date_text, strikes_out):
        _not_ready("choose a contract")

    def find_contract(self, underlying, put_call, strike, expiry_date_text):
        _not_ready("resolve a contract")

    def recent_traded_price(self, identifier):
        _not_ready("read a traded price")

    def minute_bars(self, identifier, *, begin, end):
        _not_ready("read minute bars")

    def positions(self):
        _not_ready("read positions")

    def orders(self, limit):
        _not_ready("read orders")

    def open_orders(self):
        _not_ready("read open orders")

    def filled_orders(self, start, end):
        _not_ready("read filled orders")

    def order(self, order_id):
        _not_ready("read an order")

    def attached_legs(self, order_id):
        _not_ready("read bracket legs")

    def cancel_order(self, order_id):
        _not_ready("cancel an order")

    def buy_option_with_bracket(self, settings, contract, quote, quantity, **kwargs):
        _not_ready("place an order")

    def sell_option(self, settings, contract, quote, quantity, **kwargs):
        _not_ready("place an order")
