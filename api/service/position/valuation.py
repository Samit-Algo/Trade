"""What a position is worth, and whether it is running out of time.

Positions are valued at the BID, because a position is worth what someone will
actually pay for it. Tiger values at the last trade, which is optimistic by the
width of the spread.
"""

from __future__ import annotations

from dataclasses import dataclass

from .holdings import OptionPosition


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
