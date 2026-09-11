"""Positions held, and what they are worth.

Two concerns:

    Holdings    what Tiger says is held, and the cash behind it
    Valuation   what a position is worth right now -- which needs a BID,
                because a position is worth what someone will PAY for it

Valuation is deliberately unable to invent a price. Without a supplied bid it
returns None rather than a zero standing in for an unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from tigeropen.common.consts import Currency, Market, SecurityType
from .contract import from_tiger_expiry_format
from .core.broker import POSITIONS_LIMITER, PRIME_ASSETS_LIMITER
from .market import days_until_expiry, parse_expiry_date


# --------------------------------------------------------------------------
# HOLDINGS
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# VALUATION
# --------------------------------------------------------------------------

#: Warn below this many days to expiry. Configurable per run.
DEFAULT_EXPIRY_WARNING_DAYS = 3


@dataclass(frozen=True)
class PositionValuation:
    """One position priced at the bid, with the arithmetic shown."""

    position: OptionPosition
    current_bid: float
    bid_source_tag: str
    bid_age_seconds: float
    bid_is_stale: bool
    cost_basis: float
    current_value: float
    unrealised_pnl: float
    unrealised_pnl_percent: float | None

    @property
    def is_profitable(self) -> bool:
        """True when the position is worth more than it cost."""
        return self.unrealised_pnl > 0


def calculate_cost_basis(
    average_cost: float,
    multiplier: float,
    quantity: float,
) -> float:
    """Work out what the position cost in cash.

    average_cost is per share and includes commission, so this is the real
    all-in figure rather than the premium alone. On a cheap contract the
    commission is a large fraction of it: a $28 fill came back with an
    average cost of 0.3102 per share, which is $31.02 all in.

    Args:
        average_cost: Per-share cost including commission.
        multiplier: Shares per contract.
        quantity: Contracts held.

    Returns:
        The cash the position cost, rounded to cents.
    """
    return round(average_cost * multiplier * quantity, 2)


def calculate_current_value(
    current_bid: float,
    multiplier: float,
    quantity: float,
) -> float:
    """Work out what the position would raise if sold now.

    At the bid, because that is what a buyer is currently offering. Selling
    into the bid is what closing a long position actually means.

    Args:
        current_bid: The current bid, per share.
        multiplier: Shares per contract.
        quantity: Contracts held.

    Returns:
        The cash the position would raise, rounded to cents.
    """
    return round(current_bid * multiplier * quantity, 2)


def calculate_unrealised_pnl(
    current_bid: float,
    average_cost: float,
    multiplier: float,
    quantity: float,
) -> float:
    """Work out the unrealised profit or loss.

    (current_bid - average_cost) x multiplier x quantity

    Args:
        current_bid: The current bid, per share.
        average_cost: Per-share entry cost including commission.
        multiplier: Shares per contract.
        quantity: Contracts held.

    Returns:
        Profit if positive, loss if negative, rounded to cents.
    """
    difference_per_share = current_bid - average_cost
    return round(difference_per_share * multiplier * quantity, 2)


def calculate_pnl_percent(unrealised_pnl: float, cost_basis: float) -> float | None:
    """Express the profit or loss as a percentage of what was paid.

    Args:
        unrealised_pnl: The profit or loss in cash.
        cost_basis: What the position cost.

    Returns:
        The percentage, or None when the cost basis is zero.
    """
    if cost_basis == 0:
        return None
    return round((unrealised_pnl / cost_basis) * 100, 1)


def calculate_assignment_exposure(
    strike: float,
    multiplier: float,
    quantity: float,
) -> float:
    """Work out the cash an exercise would demand.

    This is the number the expiry warning exists for. An in-the-money option
    left to expire is exercised automatically, and a call exercise buys the
    shares at the strike. The cash needed is strike x multiplier x quantity,
    and it bears no relation to what the option cost.

    Args:
        strike: The strike price.
        multiplier: Shares per contract.
        quantity: Contracts held.

    Returns:
        The cash an exercise would require.
    """
    return round(strike * multiplier * quantity, 2)


def is_expiring_soon(
    days_to_expiry: int,
    threshold_days: int = DEFAULT_EXPIRY_WARNING_DAYS,
) -> bool:
    """Decide whether a position is close enough to expiry to warn about.

    Args:
        days_to_expiry: Days remaining.
        threshold_days: Warn at or below this.

    Returns:
        True when the position needs a warning.
    """
    return days_to_expiry <= threshold_days


def build_expiry_warning(
    position: OptionPosition,
    threshold_days: int = DEFAULT_EXPIRY_WARNING_DAYS,
) -> list[str]:
    """Build the warning lines for a position close to expiry.

    Says what the risk actually is. "Expires soon" invites the reader to think
    about time decay, which is the smaller problem. The bigger one is a share
    transaction they have not budgeted for.

    Args:
        position: The position.
        threshold_days: The warning threshold, for the message.

    Returns:
        Lines to print, or an empty list when no warning is needed.
    """
    if not is_expiring_soon(position.days_to_expiry, threshold_days):
        return []

    exposure = calculate_assignment_exposure(
        position.strike, position.multiplier, position.quantity
    )

    if position.days_to_expiry < 0:
        timing = f"EXPIRED {abs(position.days_to_expiry)} day(s) ago"
    elif position.days_to_expiry == 0:
        timing = "EXPIRES TODAY"
    else:
        timing = f"EXPIRES IN {position.days_to_expiry} DAY(S)"

    if position.put_call == "CALL":
        action = "buy"
        direction = "above"
    else:
        action = "sell"
        direction = "below"

    return [
        f"{timing}  --  {position.describe()}",
        "",
        "  If this is in the money at expiry it is exercised automatically.",
        f"  That would {action} {position.multiplier * position.quantity:,.0f} "
        f"shares at {position.strike:,.2f}, a share transaction of",
        f"  ${exposure:,.2f}.",
        "",
        f"  The option cost a fraction of that. The exercise does not care.",
        f"  This happens if {position.underlying} is {direction} "
        f"{position.strike:,.2f} at expiry and you have done nothing.",
        "",
        "  To avoid it: close the position before expiry, or make sure the",
        "  cash is there.",
    ]


def value_position(
    position: OptionPosition,
    bid_snapshot,
    max_age_seconds: int,
) -> PositionValuation:
    """Price one position at the bid.

    Args:
        position: The position.
        bid_snapshot: A BidSnapshot from a MarketDataProvider.
        max_age_seconds: The staleness limit for the bid.

    Returns:
        The valuation.
    """
    cost_basis = calculate_cost_basis(
        position.average_cost, position.multiplier, position.quantity
    )
    current_value = calculate_current_value(
        bid_snapshot.bid, position.multiplier, position.quantity
    )
    unrealised_pnl = calculate_unrealised_pnl(
        bid_snapshot.bid,
        position.average_cost,
        position.multiplier,
        position.quantity,
    )

    return PositionValuation(
        position=position,
        current_bid=bid_snapshot.bid,
        bid_source_tag=bid_snapshot.source_tag,
        bid_age_seconds=bid_snapshot.age_seconds(),
        bid_is_stale=bid_snapshot.is_stale(max_age_seconds),
        cost_basis=cost_basis,
        current_value=current_value,
        unrealised_pnl=unrealised_pnl,
        unrealised_pnl_percent=calculate_pnl_percent(unrealised_pnl, cost_basis),
    )
