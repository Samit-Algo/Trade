"""What is actually held in the account.

Reads only. Nothing in this file closes a position or builds an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from tigeropen.common.consts import Currency, Market, SecurityType

from ..contract import from_tiger_expiry_format
from ..core.broker import POSITIONS_LIMITER, PRIME_ASSETS_LIMITER
from ..market import days_until_expiry, parse_expiry_date


#: Options live in the securities segment, not futures ('C') or fund ('F').
SECURITIES_SEGMENT = "S"


@dataclass(frozen=True)
class OptionPosition:
    """One option position, as Tiger reports it."""

    identifier: str
    underlying: str
    expiry_date_text: str  # "YYYY-MM-DD"
    expiry_compact: str  # "yyyyMMdd", as Tiger returns it
    strike: float
    put_call: str
    multiplier: float
    quantity: float
    average_cost: float  # per share, and INCLUDING commission -- see below
    days_to_expiry: int

    #: Tiger's own figures, kept only for comparison. Neither is used to value
    #: anything here: market_price is latestPrice, and tiger_unrealised_pnl is
    #: derived from it.
    market_price_latest: float | None
    tiger_unrealised_pnl: float | None

    def describe(self) -> str:
        """Return a one-line description of the position."""
        return (
            f"{self.underlying} {self.expiry_date_text} "
            f"{self.strike:,.2f} {self.put_call}"
        )


def _read_float(source, field_name: str, default=None):
    """Read a numeric attribute, tolerating strings and absence.

    Tiger returns some numbers as strings on contract objects -- `strike`
    arrives as '360.0' here, the same quirk the strike ladder has. Converting
    on the way in stops that spreading.

    Args:
        source: The object to read from.
        field_name: The attribute name.
        default: What to return when it is missing or unreadable.

    Returns:
        The value as a float, or the default.
    """
    value = getattr(source, field_name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_position_quantity(raw_position) -> float:
    """Read how many contracts are held.

    Tiger exposes three related fields. `quantity` is documented as the
    "legacy scaled" value with `position_scale` as its decimal scale, and
    `position_qty` as the "actual decimal position quantity". The actual one
    is preferred; quantity is the fallback for anything that omits it.

    Args:
        raw_position: A Position object from get_positions.

    Returns:
        Contracts held.
    """
    actual_quantity = _read_float(raw_position, "position_qty", None)
    if actual_quantity is not None:
        return actual_quantity

    return _read_float(raw_position, "quantity", 0.0) or 0.0


def build_option_position(raw_position) -> OptionPosition | None:
    """Turn one Position object into an OptionPosition.

    Args:
        raw_position: A Position object from get_positions.

    Returns:
        The position, or None when it is not a usable option position.
    """
    contract = getattr(raw_position, "contract", None)
    if contract is None:
        return None

    identifier = getattr(contract, "identifier", None)
    if not identifier:
        return None

    expiry_compact = str(getattr(contract, "expiry", "") or "")
    if not expiry_compact:
        return None

    try:
        expiry_date_text = from_tiger_expiry_format(expiry_compact)
    except Exception:
        return None

    expiry_date = parse_expiry_date(expiry_date_text)

    # strike arrives as a STRING ('360.0'), the same quirk the strike ladder
    # has. Left unconverted it would sort and compare wrongly.
    strike = _read_float(contract, "strike", None)
    if strike is None:
        return None

    multiplier = _read_float(contract, "multiplier", 100.0) or 100.0

    return OptionPosition(
        identifier=str(identifier).strip(),
        underlying=str(getattr(contract, "symbol", "") or ""),
        expiry_date_text=expiry_date_text,
        expiry_compact=expiry_compact,
        strike=strike,
        put_call=str(getattr(contract, "put_call", "") or "").upper(),
        multiplier=multiplier,
        quantity=read_position_quantity(raw_position),
        # Per share, and including commission. Not the premium alone.
        average_cost=_read_float(raw_position, "average_cost", 0.0) or 0.0,
        days_to_expiry=days_until_expiry(expiry_date),
        # Kept for comparison only. market_price is latestPrice, and Tiger's
        # unrealized_pnl is derived from it, so neither values anything here.
        market_price_latest=_read_float(raw_position, "market_price", None),
        tiger_unrealised_pnl=_read_float(raw_position, "unrealized_pnl", None),
    )


def list_option_positions(trade_client, account: str | None = None) -> list[OptionPosition]:
    """List the option positions currently held.

    sec_type must be passed explicitly: get_positions defaults to STK, so
    calling it without this returns shares and no options at all.

    Args:
        trade_client: A tigeropen TradeClient.
        account: Account ID, or None for the configured default.

    Returns:
        Option positions, soonest expiry first.
    """
    POSITIONS_LIMITER.wait()

    raw_positions = trade_client.get_positions(
        account=account,
        sec_type=SecurityType.OPT,
        currency=Currency.ALL,
        market=Market.US,
    )

    if not raw_positions:
        return []

    positions = []
    for raw_position in raw_positions:
        position = build_option_position(raw_position)
        if position is None:
            continue
        if position.quantity == 0:
            continue
        positions.append(position)

    positions.sort(key=lambda item: item.days_to_expiry)
    return positions


def fetch_cash_available(trade_client, account: str | None = None) -> float | None:
    """Fetch the cash available to trade, deliberately NOT buying power.

    This Reg T margin account reports roughly four times its cash as buying
    power, and the difference is borrowed. An option can go to zero on its own;
    a loan taken to buy it does not. Every affordability check in this project
    compares against cash.

    Args:
        trade_client: A tigeropen TradeClient.
        account: Account ID, or None for the configured default.

    Returns:
        Cash available to trade, or None if it could not be read.
    """
    PRIME_ASSETS_LIMITER.wait()

    try:
        portfolio = trade_client.get_prime_assets(account=account, base_currency="USD")
    except Exception:
        return None

    segments = getattr(portfolio, "segments", None) or {}
    segment = segments.get(SECURITIES_SEGMENT)
    if segment is None:
        return None

    cash = getattr(segment, "cash_available_for_trade", None)
    if cash is None:
        return None

    return float(cash)
