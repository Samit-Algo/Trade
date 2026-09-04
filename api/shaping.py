"""Turning library dataclasses into response models.

Kept out of the route handlers so a handler stays three lines: validate, call
the library, shape the response. Nothing here decides anything; it only
translates.
"""

from __future__ import annotations

from tiger_backend.market import calculate_spread, is_low_liquidity
from tiger_backend.orders import (
    BracketLegs,
    calculate_intended_risk,
    estimate_commission_per_order,
    estimate_commission_per_share,
    estimate_round_trip_commission,
    normalise_status,
)
from tiger_backend.positions import (
    calculate_assignment_exposure,
    calculate_cost_basis,
    is_expiring_soon,
)

from .models import (
    BracketOut,
    CommissionOut,
    ContractOut,
    CostOut,
    FillOutcomeOut,
    LiquidityOut,
    OrderLegOut,
    PositionOut,
    PositionValuationOut,
    QuoteOut,
)


def shape_contract(contract) -> ContractOut:
    """Convert an OptionContractInfo to its response model."""
    return ContractOut(
        identifier=contract.identifier,
        underlying=contract.underlying,
        name=contract.name,
        expiry=contract.expiry_date_text,
        expiry_compact=contract.expiry_compact,
        strike=contract.strike,
        option_type=contract.put_call,
        multiplier=contract.multiplier,
        contract_id=contract.contract_id,
        days_to_expiry=contract.days_to_expiry,
        min_tick=contract.min_tick,
    )


def shape_quote(quote) -> QuoteOut:
    """Convert a QuoteSnapshot to its response model."""
    spread, spread_percent = calculate_spread(quote.bid, quote.ask)
    return QuoteOut(
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        open_interest=quote.open_interest,
        limit_price=quote.limit_price,
        spread=spread,
        spread_percent=spread_percent,
        source=quote.source.value,
        captured_at=quote.captured_at,
        last_close=quote.last_close,
        last_close_ratio=quote.last_close_ratio,
    )


def shape_cost(estimate) -> CostOut:
    """Convert a CostEstimate to its response model."""
    return CostOut(
        price_used=estimate.price_used,
        price_reason=estimate.price_reason,
        quantity=estimate.quantity,
        multiplier=estimate.multiplier,
        shares_of_exposure=estimate.shares_of_exposure,
        total_cash=estimate.total_cash,
        cash_label=estimate.cash_label,
        break_even_price=estimate.break_even_price,
        maximum_loss=estimate.maximum_loss,
        maximum_loss_note=estimate.maximum_loss_note,
    )


def shape_liquidity(quote, threshold: int) -> LiquidityOut:
    """Describe how thinly traded the contract is."""
    return LiquidityOut(
        volume=quote.volume,
        open_interest=quote.open_interest,
        is_thin=is_low_liquidity(quote.volume, quote.open_interest, threshold),
        threshold=threshold,
    )


def shape_commission(quantity: int, multiplier: float) -> CommissionOut:
    """Describe the estimated commission for an order of this size."""
    return CommissionOut(
        per_order=estimate_commission_per_order(quantity),
        round_trip=estimate_round_trip_commission(quantity),
        per_share=round(estimate_commission_per_share(quantity, multiplier), 4),
        basis=(
            "Estimate, fitted to four measured orders: $2.985 base plus $0.035 "
            "per contract, each way. The base dominates, so a small position "
            "pays a large percentage."
        ),
    )


def shape_bracket(
    take_profit_price: float,
    stop_loss_price: float,
    leg_time_in_force: str,
    entry_price: float,
    quantity: int,
    multiplier: float,
) -> BracketOut:
    """Describe the attached legs a preview would submit."""
    legs = BracketLegs(take_profit_price, stop_loss_price, leg_time_in_force)
    round_trip = estimate_round_trip_commission(quantity)
    profit_at_target = round(
        (take_profit_price - entry_price) * multiplier * quantity - round_trip, 2
    )

    return BracketOut(
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        leg_time_in_force=leg_time_in_force,
        attach_type=legs.attach_type,
        intended_risk=calculate_intended_risk(
            entry_price, stop_loss_price, quantity, multiplier
        ),
        profit_at_target=profit_at_target,
    )


def shape_fill(outcome) -> FillOutcomeOut:
    """Convert a FillOutcome to its response model."""
    return FillOutcomeOut(
        order_id=outcome.order_id,
        status=outcome.status,
        outcome=outcome.outcome,
        requested_quantity=outcome.requested_quantity,
        filled_quantity=outcome.filled_quantity,
        average_fill_price=outcome.average_fill_price,
        actual_cash=outcome.actual_cash,
        settled=outcome.reached_terminal_status,
        poll_attempts=outcome.poll_attempts,
        broker_reason=outcome.reason or None,
    )


def shape_leg(raw_leg: dict) -> OrderLegOut:
    """Convert one entry from get_attached_legs to its response model.

    The two legs report their price in different fields: a stop is an STP order
    carrying aux_price, a take-profit is an LMT carrying limit_price. Reading
    the wrong one returns None, so both are checked here rather than in a route.
    """
    order_type = raw_leg.get("order_type")

    if order_type == "STP":
        leg_kind = "STOP_LOSS"
        price = raw_leg.get("aux_price")
    elif order_type == "LMT":
        leg_kind = "TAKE_PROFIT"
        price = raw_leg.get("limit_price")
    else:
        leg_kind = "UNKNOWN"
        price = raw_leg.get("limit_price") or raw_leg.get("aux_price")

    status = raw_leg.get("status")

    return OrderLegOut(
        order_id=raw_leg.get("id"),
        leg_kind=leg_kind,
        order_type=order_type,
        price=price,
        time_in_force=raw_leg.get("time_in_force"),
        status=normalise_status(status) if status else None,
    )


def shape_position(position, threshold_days: int, valuation=None) -> PositionOut:
    """Convert an OptionPosition, optionally with its valuation."""
    valuation_out = None
    if valuation is not None:
        valuation_out = PositionValuationOut(
            current_bid=valuation.current_bid,
            source=valuation.bid_source_tag.strip("[]"),
            current_value=valuation.current_value,
            unrealised_pnl=valuation.unrealised_pnl,
            unrealised_pnl_percent=valuation.unrealised_pnl_percent,
        )

    return PositionOut(
        identifier=position.identifier,
        underlying=position.underlying,
        expiry=position.expiry_date_text,
        strike=position.strike,
        option_type=position.put_call,
        quantity=position.quantity,
        multiplier=position.multiplier,
        average_cost=position.average_cost,
        cost_basis=calculate_cost_basis(
            position.average_cost, position.multiplier, position.quantity
        ),
        days_to_expiry=position.days_to_expiry,
        expiring_soon=is_expiring_soon(position.days_to_expiry, threshold_days),
        assignment_exposure=calculate_assignment_exposure(
            position.strike, position.multiplier, position.quantity
        ),
        tiger_unrealised_pnl=position.tiger_unrealised_pnl,
        valuation=valuation_out,
    )
