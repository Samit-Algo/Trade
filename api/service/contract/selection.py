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

from .errors import (
    ExpiryNotListedError,
    StrikeNotFoundError,
    SymbolNotListedError,
)
from .identifiers import to_tiger_expiry_format, validate_option_type
from .resolve import find_option_contract, list_strikes_for_expiry
from ..market import list_expirations

#: Do not trade something expiring today or tomorrow by default. An option a
#: day from expiry loses value fast and gaps hard, and `find_next_tradable_
#: expiry` in resolve.py would happily return one -- it only asks whether the
#: date has passed, which is the right question for validation and the wrong
#: one for selection.
DEFAULT_MIN_DAYS_TO_EXPIRY = 3

#: How many whole strikes out of the money to go by default. 1 is the first
#: whole strike past the spot price. In-the-money strikes are never chosen.
DEFAULT_STRIKES_OUT = 1

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


def select_contract(
    quote_client,
    trade_client,
    symbol: str,
    option_type: str,
    current_price: float,
    minimum_days: int = DEFAULT_MIN_DAYS_TO_EXPIRY,
    expiry_date_text: str | None = None,
    strikes_out: int = DEFAULT_STRIKES_OUT,
):
    """Turn a symbol, a side and a spot price into one verified contract.

    The whole job in one call: list the expiries, pick one, list that expiry's
    strikes, pick the nearest, then hand the result to Phase 3 to be verified
    against Tiger. Three lookups, none of which needs a market-data
    entitlement.

    Args:
        quote_client: A tigeropen QuoteClient.
        trade_client: A tigeropen TradeClient.
        symbol: The underlying, e.g. "AAPL".
        option_type: "CALL" or "PUT".
        current_price: The underlying's price. Used ONLY to pick the strike;
            it is never treated as an option price.
        minimum_days: Refuse an auto-picked expiry sooner than this.
        expiry_date_text: An explicit expiry as "YYYY-MM-DD". When given it is
            used as-is and `minimum_days` is not applied -- naming a date is a
            deliberate act, so it is not second-guessed. It is still verified
            against Tiger, which refuses an expired or unlisted one.
        strikes_out: How many WHOLE strikes out of the money to go. In-the-money
            strikes are never chosen.

    Returns:
        A triple of (OptionContractInfo, why this expiry, why this strike).
        The two sentences exist so a surprising fill can be traced back to the
        decision that caused it.

    Raises:
        SymbolNotListedError: If Tiger lists no options for the symbol.
        ExpiryNotListedError: If nothing is far enough out.
        StrikeNotFoundError: If the expiry has no strikes on that side.
        ContractError: If the chosen contract fails verification.
    """
    normalised = symbol.strip().upper()
    side = validate_option_type(option_type)

    expiries = list_expirations(quote_client, normalised)
    if not expiries:
        raise SymbolNotListedError(
            f"Tiger lists no option expirations for {normalised!r}. Either the "
            "symbol is wrong or it has no listed options."
        )

    if expiry_date_text:
        matching = [e for e in expiries if e.date_text == expiry_date_text]
        if not matching:
            raise ExpiryNotListedError(
                f"Tiger does not list {expiry_date_text!r} for {normalised}. "
                f"The nearest listed dates are: "
                + ", ".join(e.date_text for e in expiries[:5])
            )
        expiry = matching[0]
        expiry_reason = f"chosen by the caller ({expiry.days_to_expiry} days out)"
    else:
        expiry, expiry_reason = choose_expiry(expiries, minimum_days=minimum_days)

    compact = to_tiger_expiry_format(expiry.date_text)

    strikes = list_strikes_for_expiry(trade_client, normalised, compact, side)
    strike, strike_reason = find_otm_whole_strike(
        strikes, current_price, side, strikes_out
    )

    # Phase 3, unchanged. It re-checks the expiry and asks Tiger to confirm the
    # contract. That repeats the expiry lookup, which is the price of having
    # one verification path rather than two; callers cache the whole result.
    contract = find_option_contract(
        quote_client, trade_client, normalised, side, strike, expiry.date_text
    )
    return contract, expiry_reason, strike_reason



def is_whole_strike(strike: float) -> bool:
    """True when a strike has no fractional part.

    Deliberately not `strike % 5 == 0`. Ladder spacing varies with the price
    of the underlying -- 2.5 on TSLA, 0.5 on a $40 stock, 1.0 on a $30 one --
    so a hardcoded 5 would filter every strike off a cheap stock and leave
    nothing to choose from. Asking for a whole number adapts on its own.

    Args:
        strike: The strike to test.

    Returns:
        Whether it is a round number.
    """
    return float(strike) == int(strike)


def find_otm_whole_strike(
    available_strikes: list[float],
    spot_price: float,
    put_call: str,
    strikes_out: int = DEFAULT_STRIKES_OUT,
) -> tuple[float, str]:
    """Pick the Nth whole-number strike that is out of the money.

    Three filters, in order: whole numbers, out of the money, then the Nth one
    counting away from the spot price. A strike sitting exactly ON the spot is
    at the money, not out of it, so it is skipped.

    OUT OF THE MONEY means above the spot for a CALL and below it for a PUT.

    Args:
        available_strikes: Strikes Tiger lists, ascending.
        spot_price: The underlying's price.
        put_call: "CALL" or "PUT".
        strikes_out: 1 for the first strike out, 2 for the second.

    Returns:
        A pair of (the strike, a sentence saying which rule chose it).

    Raises:
        StrikeNotFoundError: If nothing is out of the money at all.
    """
    if not available_strikes:
        raise StrikeNotFoundError(
            "Tiger listed no strikes for that expiry and side."
        )

    def out_of_money(strikes):
        """Strikes past the spot, nearest first."""
        if put_call == "CALL":
            return sorted(k for k in strikes if k > spot_price)
        return sorted((k for k in strikes if k < spot_price), reverse=True)

    wanted = max(1, strikes_out)
    whole_otm = out_of_money(
        [k for k in available_strikes if is_whole_strike(k)]
    )

    if whole_otm:
        index = min(wanted - 1, len(whole_otm) - 1)
        chosen = whole_otm[index]
        if index < wanted - 1:
            return chosen, (
                f"wanted strike {wanted} out of the money but only "
                f"{len(whole_otm)} whole strike(s) exist past "
                f"{spot_price:,.2f}; took the furthest, {chosen:,.2f}"
            )
        return chosen, (
            f"whole strike {wanted} out of the money for a {put_call}, "
            f"{chosen:,.2f} against a spot of {spot_price:,.2f}"
        )

    # No whole strike past the spot. A less round strike beats no trade, but
    # say so plainly -- half strikes are thinner and the fill will show it.
    any_otm = out_of_money(available_strikes)
    if any_otm:
        chosen = any_otm[min(wanted - 1, len(any_otm) - 1)]
        return chosen, (
            f"NO WHOLE strike is out of the money past {spot_price:,.2f}; "
            f"fell back to {chosen:,.2f}"
        )

    raise StrikeNotFoundError(
        f"No strike is out of the money for a {put_call} at "
        f"{spot_price:,.2f}. The listed strikes run "
        f"{min(available_strikes):,.2f} to {max(available_strikes):,.2f}."
    )
