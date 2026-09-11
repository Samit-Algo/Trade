"""OPTIONAL: contract quantity that scales with the option's premium.

=============================================================================
 THIS WHOLE FEATURE IS REMOVABLE. To turn it off, set in .env:

     QUANTITY_TIERS_ENABLED=false

 To delete it outright: remove this file, the `quantity_tiers` lines in
 `order/__init__.py`, the `quantity_tiers_enabled` and `quantity_tiers`
 settings in `core/config.py`, and the `resolve_quantity` call in
 `routes/trade.py`. Nothing else references it, and `TRADE_QUANTITY` goes
 back to being the only rule.
=============================================================================

WHY. A flat quantity spends wildly different amounts. At TRADE_QUANTITY=1 a
$0.40 option is $40 of exposure and a $7.50 one is $750 -- the same "one
trade" meaning nearly twenty times the money. Scaling the quantity by premium
makes the CASH per trade roughly even instead.

THE BANDS ARE SIZED FOR MAX_TRADE_CASH. Each band's top premium times its
quantity lands at or under the cap, so the tier can never propose an order the
cap would then refuse:

    premium        qty     cash at top of band
    < 1.00          5      $500
    1.00 - 1.60     4      $640
    1.60 - 2.65     3      $795
    2.65 - 4.00     2      $800
    >= 4.00         1      $800 at a $8.00 premium

THE QUANTITY IS NEVER ADJUSTED AFTERWARDS. What the band says is what is
ordered. An order that would still exceed the cap -- only possible above an
$8.00 premium -- is refused by MAX_TRADE_CASH rather than quietly shrunk,
because silently trading a different size than the table states is the kind of
surprise this project avoids everywhere else.

KEEP THE TWO IN STEP. These bands are numbers in .env, not derived from the
cap. Lowering MAX_TRADE_CASH without redrawing QUANTITY_TIERS leaves bands
that propose more than the cap allows, and those orders will be refused. The
check in core/config.py catches this at startup rather than at trade time.
"""

from __future__ import annotations

#: Upper bound (exclusive) of each premium band, with the quantity to trade.
#: Ordered low to high; the first band whose bound the premium is under wins.
#: A premium at or above every bound falls to DEFAULT_TOP_TIER_QUANTITY.
DEFAULT_TIERS: tuple[tuple[float, int], ...] = (
    (1.00, 5),
    (1.60, 4),
    (2.65, 3),
    (4.00, 2),
)

#: Applied above the highest band.
DEFAULT_TOP_TIER_QUANTITY = 1


def quantity_for_premium(
    premium: float,
    tiers: tuple[tuple[float, int], ...] = DEFAULT_TIERS,
    top_tier_quantity: int = DEFAULT_TOP_TIER_QUANTITY,
) -> int:
    """Return the tiered quantity for a premium, in whole contracts.

    Args:
        premium: The option premium, per share.
        tiers: (upper bound exclusive, quantity), lowest bound first.
        top_tier_quantity: Used when the premium is above every bound.

    Returns:
        The number of contracts to trade.
    """
    for upper_bound, quantity in tiers:
        if premium < upper_bound:
            return quantity
    return top_tier_quantity


def resolve_quantity(
    premium: float,
    fallback_quantity: int,
    enabled: bool,
    tiers: tuple[tuple[float, int], ...] = DEFAULT_TIERS,
    top_tier_quantity: int = DEFAULT_TOP_TIER_QUANTITY,
) -> tuple[int, str]:
    """Decide how many contracts to trade, and say why.

    The reason string is returned rather than logged so it can reach the API
    response: a quantity that changes per trade is invisible otherwise, and an
    unexplained size is exactly what makes a surprising fill hard to trace
    afterwards.

    Args:
        premium: The snapped entry premium, in dollars.
        fallback_quantity: TRADE_QUANTITY, used when tiers are off.
        enabled: False to ignore the tiers entirely.
        tiers: (upper bound exclusive, quantity), lowest bound first.
        top_tier_quantity: Used when the premium is above every bound.

    Returns:
        The quantity, and one sentence explaining it.
    """
    if not enabled:
        return fallback_quantity, (
            f"{fallback_quantity} from TRADE_QUANTITY; premium tiers are off."
        )

    quantity = quantity_for_premium(premium, tiers, top_tier_quantity)

    band = _describe_band(premium, tiers)
    return quantity, (
        f"{quantity} from the premium tiers: {premium:.2f} falls in {band}."
    )


def _describe_band(
    premium: float, tiers: tuple[tuple[float, int], ...]
) -> str:
    """Name the band a premium falls in, for the reason string.

    Args:
        premium: The premium being placed.
        tiers: The bands in force.

    Returns:
        A human-readable band, e.g. "1.00-1.60" or "4.00 and above".
    """
    lower = 0.0
    for upper_bound, _quantity in tiers:
        if premium < upper_bound:
            return f"{lower:.2f}-{upper_bound:.2f}"
        lower = upper_bound
    return f"{lower:.2f} and above"
