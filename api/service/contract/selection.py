"""Which contract, when the caller did not name one?

Pure arithmetic over lists Tiger has already returned. Nothing here touches
the network, and nothing here invents a strike or a date: both come from the
broker's own ladder.

The fast trading API sends only a symbol, a side and the underlying's price.
Something has to turn that into one contract. These two functions are that
something, and they are deliberately separate from `resolve.py` because they
answer a different question: `resolve.py` verifies a contract you named, this
file chooses which one to name.
"""

from __future__ import annotations

from .errors import ExpiryNotListedError, StrikeNotFoundError

#: Do not trade something expiring today or tomorrow by default. An option a
#: day from expiry loses value fast and gaps hard, and `find_next_tradable_
#: expiry` in resolve.py would happily return one -- it only asks whether the
#: date has passed, which is the right question for validation and the wrong
#: one for selection.
DEFAULT_MIN_DAYS_TO_EXPIRY = 3

#: Monthlies carry the volume, and thin contracts are the ones that are hard
#: to get out of. Preferred, not required: a symbol with no monthly inside the
#: window still gets a contract rather than an error.
MONTHLY_TAG = "m"


def choose_expiry(
    expiries,
    minimum_days: int = DEFAULT_MIN_DAYS_TO_EXPIRY,
    prefer_monthly: bool = True,
):
    """Pick which expiry to trade from the ones Tiger lists.

    Args:
        expiries: OptionExpiry objects, soonest first.
        minimum_days: Refuse anything expiring sooner than this.
        prefer_monthly: Take the soonest monthly in the window when one exists.

    Returns:
        A pair of (the chosen OptionExpiry, a sentence saying why).

    Raises:
        ExpiryNotListedError: If nothing satisfies the minimum.
    """
    tradable = [
        expiry for expiry in expiries if expiry.days_to_expiry >= minimum_days
    ]

    if not tradable:
        soonest = min(
            (e.days_to_expiry for e in expiries if e.days_to_expiry >= 0),
            default=None,
        )
        raise ExpiryNotListedError(
            f"No expiry is at least {minimum_days} days away. "
            + (
                f"The soonest tradable one is {soonest} day(s) out."
                if soonest is not None
                else "Nothing listed has yet to expire."
            )
        )

    if prefer_monthly:
        for expiry in tradable:
            if expiry.period_tag == MONTHLY_TAG:
                return expiry, (
                    f"soonest monthly at least {minimum_days} days out "
                    f"({expiry.days_to_expiry} days)"
                )

    chosen = tradable[0]
    return chosen, (
        f"soonest expiry at least {minimum_days} days out "
        f"({chosen.days_to_expiry} days, {chosen.period_label})"
    )


def find_closest_strike(
    available_strikes: list[float],
    target_price: float,
    put_call: str,
) -> tuple[float, str]:
    """Pick the single listed strike nearest to a price.

    `find_nearest_strikes` in resolve.py looks similar and is not
    interchangeable: it returns several strikes sorted ascending, for putting
    in an error message, which throws away which one was actually closest.

    Args:
        available_strikes: Strikes Tiger lists, ascending.
        target_price: Usually the underlying's current price.
        put_call: "CALL" or "PUT", used only to break an exact tie.

    Returns:
        A pair of (the strike, a sentence saying why).

    Raises:
        StrikeNotFoundError: If there are no strikes to choose from.
    """
    if not available_strikes:
        raise StrikeNotFoundError(
            "Tiger listed no strikes for that expiry and side, so there is "
            "nothing to choose from."
        )

    closest_distance = min(
        abs(strike - target_price) for strike in available_strikes
    )
    tied = [
        strike
        for strike in available_strikes
        if abs(abs(strike - target_price) - closest_distance) < 1e-9
    ]

    if len(tied) == 1:
        chosen = tied[0]
        return chosen, (
            f"closest of {len(available_strikes)} listed strikes to "
            f"{target_price:,.2f}"
        )

    # An exact tie means the price sits halfway between two strikes. Take the
    # out-of-the-money side: it is the cheaper contract, and picking the same
    # way every time matters more than which way.
    chosen = max(tied) if put_call == "CALL" else min(tied)
    return chosen, (
        f"{target_price:,.2f} sits exactly between {min(tied):,.2f} and "
        f"{max(tied):,.2f}; took the out-of-the-money side for a {put_call}"
    )
