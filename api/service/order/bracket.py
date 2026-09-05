"""Take-profit and stop-loss legs, and what the commission really costs.

The legs attach to a parent order and are sent with it in one call. Tiger's
own `preview_order` cannot validate them, so the checks in this file are the
ONLY thing standing between a typo and a live bracket.
"""

from __future__ import annotations

from dataclasses import dataclass

from tigeropen.common.util.contract_utils import option_contract
from tigeropen.common.util.order_utils import (
    limit_order,
    limit_order_with_legs,
    order_leg,
)
from ..core.broker import ORDERS_LIMITER
from ..market import QuoteSnapshot
from .build import DEFAULT_TIME_IN_FORCE, RULE_WIDTH, format_money
from .cost import CostEstimate
from .status import get_order_status, normalise_status
from .ticks import TickError, apply_buffer, snap_down, snap_nearest, snap_up

# ---------------------------------------------------------------------------
# Phase 7 -- attached take-profit and stop-loss legs
#
# Legs attach to a PARENT order and activate when it fills. They cannot be
# attached to a position you already hold: to bracket an existing holding you
# must close it and buy again with legs attached.
#
# The important operational fact, established by probing on 2026-09-03:
# preview_order REFUSES attached orders --
#     code=1010 biz param error(OCA/ATTACHED order preview not supported)
# for options and for stocks alike, while previewing a plain option order
# fine. So a bracketed order cannot be validated by the broker before it is
# sent. The local checks below are the only pre-submission check that exists,
# which is why they are louder than they would otherwise need to be.
# ---------------------------------------------------------------------------

# Commission model, fitted to four real paper orders placed 2026-09-03:
#
#   BUY  1 contract  @ 0.2800   commission $3.02
#   SELL 1 contract  @ 0.2800   commission $3.02
#   BUY  3 contracts @ 0.0700   commission $3.09
#   SELL 3 contracts @ 0.0600   commission $3.10
#
# Neither simple model fits. Flat would predict $3.02 for three contracts,
# out by $0.07; per-contract would predict $9.06, out by $5.97. A base fee
# plus a small per-contract component reproduces all four:
#
#   1 contract  -> 2.985 + 0.035     = $3.02
#   3 contracts -> 2.985 + 0.105     = $3.09
#
# UNMODELLED: the 3-contract SELL came back at $3.10, one cent above the
# matching buy. One observation is not enough to model it. It may be a
# proceeds-based regulatory fee, which in the US applies to sales and not to
# purchases, in which case it would scale with the money received rather than
# with contracts. Until a second sale at a different size says otherwise, the
# estimate here runs a cent light on the sell side of a multi-contract trade.
#
# The base dominates: tripling the size added seven cents. Commission is, for
# practical purposes, a fixed toll per order.
COMMISSION_BASE = 2.985
COMMISSION_PER_CONTRACT = 0.035

LEG_PROFIT = "PROFIT"
LEG_LOSS = "LOSS"


class BracketError(Exception):
    """The requested bracket prices do not make sense."""


@dataclass(frozen=True)
class BracketLegs:
    """The take-profit and stop-loss attached to a parent order."""

    take_profit_price: float
    stop_loss_price: float
    leg_time_in_force: str

    @property
    def attach_type(self) -> str:
        """Return the attach_type the SDK will put on the wire.

        Mirrors the SDK's own rule so the audit trail can record what was
        actually sent: one leg sends its own type, two send BRACKETS. See
        tigeropen/trade/request/model.py _parse_leg_param.
        """
        return "BRACKETS"


def estimate_commission_per_order(quantity: int) -> float:
    """Estimate the commission on one order of a given size.

    Args:
        quantity: Contracts.

    Returns:
        Estimated commission in cash.
    """
    return round(COMMISSION_BASE + COMMISSION_PER_CONTRACT * quantity, 2)


def estimate_commission_per_share(quantity: int, multiplier: float) -> float:
    """Spread one order's commission across the shares involved.

    Args:
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        Estimated commission per share.
    """
    shares = multiplier * quantity
    if shares <= 0:
        return 0.0
    return estimate_commission_per_order(quantity) / shares


def estimate_round_trip_commission(quantity: int) -> float:
    """Estimate commission for getting in and back out again.

    Two orders: the entry, and whichever leg closes it. On a cheap contract
    this is the largest single cost in the trade -- a round trip on one
    contract at $0.28 costs 21.6% of the premium before the market moves.

    Takes no multiplier: commission is charged per CONTRACT, not per share, so
    the shares-per-contract figure does not enter into it. That distinction is
    the whole point of the measurement above.

    Args:
        quantity: Contracts.

    Returns:
        Estimated round-trip commission in cash.
    """
    return round(estimate_commission_per_order(quantity) * 2, 2)


def is_take_profit_a_losing_exit(
    take_profit_price: float,
    entry_limit_price: float,
    quantity: int,
    multiplier: float,
) -> bool:
    """Decide whether the take-profit would actually lose money.

    A "take profit" set at or below the entry price plus the commission you
    will pay to get out is not taking a profit. It is closing at a loss with
    an encouraging name on it.

    Args:
        take_profit_price: Where the profit leg would sell.
        entry_limit_price: What the parent would pay.
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        True when the exit loses money.
    """
    commission_per_share = estimate_commission_per_share(quantity, multiplier)
    break_even_exit = entry_limit_price + commission_per_share
    return take_profit_price <= break_even_exit


def validate_bracket_prices(
    entry_limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> None:
    """Check the three prices are in a sane order.

    Args:
        entry_limit_price: What the parent pays.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.

    Raises:
        BracketError: If the prices cannot form a working bracket.
    """
    if take_profit_price <= 0 or stop_loss_price <= 0:
        raise BracketError("Bracket prices must be greater than zero.")

    if stop_loss_price >= entry_limit_price:
        raise BracketError(
            f"Stop loss {stop_loss_price:,.2f} is at or above the entry price "
            f"{entry_limit_price:,.2f}. A stop above your entry would trigger "
            "immediately on any fill."
        )

    if take_profit_price <= entry_limit_price:
        raise BracketError(
            f"Take profit {take_profit_price:,.2f} is at or below the entry "
            f"price {entry_limit_price:,.2f}. That is not a profit target."
        )

    # No separate "inverted bracket" check is needed. The two tests above
    # already force stop_loss < entry < take_profit, so take_profit is
    # necessarily above stop_loss by the time execution reaches here. A third
    # check would be unreachable, and unreachable code implies a case that
    # can happen when it cannot.


def calculate_intended_risk(
    entry_limit_price: float,
    stop_loss_price: float,
    quantity: int,
    multiplier: float,
) -> float:
    """Work out the cash the stop is intended to cap the loss at.

    Intended, not guaranteed. A stop is a trigger, not a promise: the fill can
    be worse, and on a gap there may be no fill at that price at all.

    Args:
        entry_limit_price: What the parent pays.
        stop_loss_price: Where the stop leg sells.
        quantity: Contracts.
        multiplier: Shares per contract.

    Returns:
        The intended loss in cash, including estimated round-trip commission.
    """
    price_risk = (entry_limit_price - stop_loss_price) * multiplier * quantity
    commission = estimate_round_trip_commission(quantity)
    return round(price_risk + commission, 2)


def build_option_order_with_bracket(
    settings,
    contract,
    action: str,
    quantity: int,
    limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str = DEFAULT_TIME_IN_FORCE,
    time_in_force: str = DEFAULT_TIME_IN_FORCE,
):
    """Construct a limit order with take-profit and stop-loss legs attached.

    Builds and returns. Does not submit.

    Both legs go in one list. The SDK encodes that as attach_type='BRACKETS',
    which the documented appendix lists as a valid attach type -- so the "only
    one sub-order" limit described in Tiger's app help does not apply to the
    API.

    Args:
        settings: Validated configuration, for the account number.
        contract: An OptionContractInfo, already verified.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        limit_price: The parent's limit price.
        take_profit_price: Where the profit leg sells.
        stop_loss_price: Where the stop leg sells.
        leg_time_in_force: Time in force for the LEGS. Parameterised because
            whether a paper account accepts GTC on a leg is undocumented and
            could not be established without submitting; DAY is the SDK's own
            default and the conservative choice.
        time_in_force: Time in force for the parent. Paper rejects GTC.

    Returns:
        The SDK Order object, unsent, with both legs attached.
    """
    order_contract = option_contract(
        identifier=contract.identifier,
        multiplier=contract.multiplier,
    )

    take_profit_leg = order_leg(
        LEG_PROFIT,
        take_profit_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )
    stop_loss_leg = order_leg(
        LEG_LOSS,
        stop_loss_price,
        time_in_force=leg_time_in_force,
        outside_rth=False,
    )

    order = limit_order_with_legs(
        account=settings.account,
        contract=order_contract,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        order_legs=[take_profit_leg, stop_loss_leg],
        time_in_force=time_in_force,
    )

    # Extended hours off, as everywhere else in this project.
    order.outside_rth = False

    return order


def print_bracket_preview(
    contract,
    quote: QuoteSnapshot,
    estimate: CostEstimate,
    legs: BracketLegs,
) -> None:
    """Print the bracket-specific block beneath the ordinary order preview.

    Args:
        contract: The OptionContractInfo.
        quote: The QuoteSnapshot.
        estimate: The CostEstimate for the parent.
        legs: The bracket prices.
    """
    entry_price = estimate.price_used
    quantity = estimate.quantity
    multiplier = estimate.multiplier

    round_trip_commission = estimate_round_trip_commission(quantity)
    commission_per_share = estimate_commission_per_share(quantity, multiplier)
    intended_risk = calculate_intended_risk(
        entry_price, legs.stop_loss_price, quantity, multiplier
    )

    profit_at_target = round(
        (legs.take_profit_price - entry_price) * multiplier * quantity
        - round_trip_commission,
        2,
    )

    print("-" * RULE_WIDTH)
    print("ATTACHED ORDERS (BRACKET)")
    print("-" * RULE_WIDTH)
    print(
        f"  Entry  LIMIT  : {entry_price:,.2f}   "
        "buys the contract; the legs are dormant until it fills"
    )
    print(
        f"  Take profit   : {legs.take_profit_price:,.2f}   "
        "sells if the price rises to here"
    )
    print(
        f"  Stop loss     : {legs.stop_loss_price:,.2f}   "
        "sells if the price falls to here"
    )
    print(f"  Leg time in force : {legs.leg_time_in_force}")
    print(f"  attach_type sent  : {legs.attach_type}")
    print("-" * RULE_WIDTH)
    print(
        f"  Est. round-trip commission : ${round_trip_commission:,.2f}"
        f"  (${commission_per_share:.4f}/share)"
    )
    print("    ESTIMATE. Fitted to four real orders: $2.985 base plus")
    print("    $0.035 per contract, each way. The base dominates, so a")
    print("    small position pays a large percentage.")
    print("-" * RULE_WIDTH)
    print(f"  If the stop triggers : lose about ${intended_risk:,.2f}")
    print(f"  If the target hits   : make about ${profit_at_target:,.2f}")
    print(
        f"  CASH AT RISK         : {format_money(estimate.total_cash)}"
        "   (the whole premium)"
    )
    print("    The stop is a trigger, not a promise. On a gap the fill can be")
    print("    worse than the stop price, or there may be no fill at all, so")
    print("    the full premium is still what you are risking.")
    print("-" * RULE_WIDTH)

    if is_take_profit_a_losing_exit(
        legs.take_profit_price, entry_price, quantity, multiplier
    ):
        break_even_exit = entry_price + commission_per_share
        print("!" * RULE_WIDTH)
        print("!!  THE TAKE PROFIT IS A LOSING EXIT")
        print("!" * RULE_WIDTH)
        print(
            f"!!  Selling at {legs.take_profit_price:,.2f} after paying "
            f"{entry_price:,.2f} does not"
        )
        print("!!  cover the commission to get back out.")
        print(
            f"!!  You need at least {break_even_exit:,.4f} to break even "
            "(estimated)."
        )
        print("!!  A 'take profit' below break-even takes a loss.")
        print("!" * RULE_WIDTH)

    print("  NOT VALIDATED BY THE BROKER.")
    print("    Tiger refuses to preview attached orders:")
    print("    code=1010 'OCA/ATTACHED order preview not supported'.")
    print("    A plain order can be checked before sending; this cannot.")
    print("    The checks above are the only pre-submission check there is.")
    print("-" * RULE_WIDTH)


def get_attached_legs(trade_client, parent_order_id: int) -> list:
    """Find the legs attached to a parent order.

    Tried two ways, because which one carries the legs is not documented:
    the parent's own `order_legs` attribute, and any order reporting this one
    as its `parent_id`.

    Args:
        trade_client: A tigeropen TradeClient.
        parent_order_id: The parent order's global ID.

    Returns:
        A list of dicts describing what was found, empty if nothing was.
    """
    from tigeropen.common.consts import Market

    found = []

    parent = get_order_status(trade_client, parent_order_id)
    if parent is not None:
        for leg in getattr(parent, "order_legs", None) or []:
            found.append(
                {
                    "source": "parent.order_legs",
                    "leg_type": getattr(leg, "leg_type", None),
                    "price": getattr(leg, "price", None),
                    "time_in_force": getattr(leg, "time_in_force", None),
                    "outside_rth": getattr(leg, "outside_rth", None),
                }
            )

    ORDERS_LIMITER.wait()
    try:
        recent_orders = trade_client.get_orders(limit=50, market=Market.US)
    except Exception:
        recent_orders = None

    for candidate in recent_orders or []:
        if getattr(candidate, "parent_id", None) == parent_order_id:
            found.append(
                {
                    "source": "child order",
                    "id": getattr(candidate, "id", None),
                    "order_type": getattr(candidate, "order_type", None),
                    "action": getattr(candidate, "action", None),
                    "quantity": getattr(candidate, "quantity", None),
                    "limit_price": getattr(candidate, "limit_price", None),
                    "aux_price": getattr(candidate, "aux_price", None),
                    "time_in_force": getattr(candidate, "time_in_force", None),
                    "status": normalise_status(getattr(candidate, "status", None)),
                }
            )

    return found


def print_attached_legs(legs_found: list, parent_order_id: int) -> None:
    """Print what was found attached to a parent order.

    Args:
        legs_found: The result of get_attached_legs.
        parent_order_id: The parent's ID, for the heading.
    """
    print("=" * RULE_WIDTH)
    print(f"  ATTACHED LEGS ON ORDER {parent_order_id}")
    print("=" * RULE_WIDTH)

    if not legs_found:
        print("  None found.")
        print("")
        print("  That does not by itself prove the legs were rejected. It may")
        print("  mean the legs are not exposed through either route tried")
        print("  (the parent's order_legs, or a child order naming this parent).")
        print("  Check the Tiger app before concluding anything.")
        print("=" * RULE_WIDTH)
        return

    for item in legs_found:
        print(f"  via {item.pop('source')}")
        for key, value in item.items():
            if value is not None:
                print(f"    {key:<16}= {value}")
        print("")

    print("=" * RULE_WIDTH)


# ---------------------------------------------------------------------------
# Phase 10 -- turning percentages into legal bracket prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BracketCalculation:
    """Every number behind a bracket, so the caller can show its working.

    Both the raw arithmetic and the rounded result are kept. A response that
    showed only the final prices would leave a reader unable to tell a
    deliberate rounding from a bug.
    """

    tick_size: float
    buffer_ticks: int

    entry_requested: float      # what the caller sent
    entry_snapped: float        # after snapping onto the grid
    entry_actual: float         # after the buffer -- this is the BUY limit

    take_profit_percent: float
    take_profit_raw: float      # before rounding
    take_profit_price: float    # rounded UP

    stop_loss_percent: float
    stop_loss_raw: float        # before rounding
    stop_loss_price: float      # rounded DOWN

    @property
    def rounding_note(self) -> str:
        """Explain the rounding directions in one line."""
        return (
            "Take-profit rounds up and stop-loss rounds down, so rounding "
            "only ever widens the bracket. Neither leg can fire sooner, or at "
            "a worse price, than was asked for."
        )


def calculate_bracket_from_percentages(
    entry_price: float,
    take_profit_percent: float,
    stop_loss_percent: float,
    tick_size: float,
    buffer_ticks: int,
) -> BracketCalculation:
    """Turn a caller's entry price and two percentages into legal prices.

    The percentages are applied to the BUFFERED entry, not the price that
    arrived, because the buffered price is what will actually be paid. Taking
    20% of a price you are not paying would describe a different trade.

    Rounding direction is not symmetric, and that is the point. Take-profit
    rounds up because rounding a target down sells for less than was asked.
    Stop-loss rounds down because rounding a stop up triggers it sooner, and
    at a worse price, than was asked. Both move away from the entry.

    Args:
        entry_price: The option premium the caller supplied.
        take_profit_percent: Percent above the buffered entry, e.g. 20.
        stop_loss_percent: Percent below the buffered entry, e.g. 15.
        tick_size: The valid price increment.
        buffer_ticks: Whole ticks to add to the entry, to help it fill.

    Returns:
        Every intermediate value, for both the order and the response.

    Raises:
        TickError: If the tick size or buffer is unusable.
        BracketError: If the rounded stop loss lands at or below zero.
    """
    entry_snapped = snap_nearest(entry_price, tick_size)
    entry_actual = apply_buffer(entry_snapped, tick_size, buffer_ticks)

    if entry_actual <= 0:
        raise TickError(
            f"The entry price rounded to {entry_actual}, which cannot be "
            "placed. The price supplied was below half of one tick."
        )

    take_profit_raw = entry_actual * (1.0 + take_profit_percent / 100.0)
    stop_loss_raw = entry_actual * (1.0 - stop_loss_percent / 100.0)

    take_profit_price = snap_up(take_profit_raw, tick_size)
    stop_loss_price = snap_down(stop_loss_raw, tick_size)

    # A stop that rounds down to nothing is not a stop. This bites on very
    # cheap contracts: a 15% stop on a $0.01 option floors straight to zero.
    if stop_loss_price < tick_size:
        raise BracketError(
            f"A {stop_loss_percent:g}% stop below {entry_actual:,.2f} works "
            f"out at {stop_loss_raw:,.4f}, which rounds down to "
            f"{stop_loss_price:,.2f} -- below the minimum increment of "
            f"{tick_size:,.2f}. Use a smaller stop percentage, or a contract "
            "that is not this cheap."
        )

    return BracketCalculation(
        tick_size=tick_size,
        buffer_ticks=buffer_ticks,
        entry_requested=entry_price,
        entry_snapped=entry_snapped,
        entry_actual=entry_actual,
        take_profit_percent=take_profit_percent,
        take_profit_raw=round(take_profit_raw, 6),
        take_profit_price=take_profit_price,
        stop_loss_percent=stop_loss_percent,
        stop_loss_raw=round(stop_loss_raw, 6),
        stop_loss_price=stop_loss_price,
    )
