"""Which exact contract are we talking about?

Identity only. Resolving a contract needs no market-data entitlement, which is
what lets the rest of this project work without one.

Four concerns, in dependency order:

    The four ways resolution fails    ContractError and its subclasses
    Identifiers                       a contract as a string, and back
    Resolution                        does it exist, is it expired, what is
                                      its ID -- this is what talks to Tiger
    Selection                         which expiry and strike to trade

Read it top to bottom: nothing here refers to anything below it.
"""

from __future__ import annotations

from datetime import datetime
from tigeropen.common.util.contract_utils import (


    extract_option_info,
    get_option_identifier,
)
from dataclasses import dataclass
from tigeropen.common.consts import Currency, SecurityType
from .core.broker import CONTRACT_LIMITER, DERIVATIVE_CONTRACTS_LIMITER
from .market import MarketDataError, days_until_expiry, list_expirations
from .market import list_expirations


# --------------------------------------------------------------------------
# ERRORS
# --------------------------------------------------------------------------

class ContractError(Exception):
    """A contract could not be resolved. Always carries an actionable message."""


class ExpiryNotListedError(ContractError):
    """The requested expiry is not one Tiger lists for this underlying."""


class ExpiredContractError(ContractError):
    """The expiry is listed, but the date has already passed.

    Kept separate from ExpiryNotListedError on purpose. Reporting an expired
    contract as "not found" sends the reader hunting for a typo in a date that
    is real, correct, and was tradable yesterday.
    """


class StrikeNotFoundError(ContractError):
    """No contract exists at that strike for that expiry and side."""


class SymbolNotListedError(ContractError):
    """Tiger lists no options at all for this underlying.

    Different from an expiry or strike being wrong: the symbol itself is not
    something you can trade options on here, so suggesting a nearer date or
    strike would be nonsense.
    """


# --------------------------------------------------------------------------
# IDENTIFIERS
# --------------------------------------------------------------------------

VALID_OPTION_TYPES = ("CALL", "PUT")


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------


def to_tiger_expiry_format(date_text: str) -> str:
    """Convert "YYYY-MM-DD" to the "yyyyMMdd" form Tiger's contract calls want.

    Tiger's expiration list returns the dashed form and its contract functions
    require the compact one. This mismatch is the single most common bug in
    this integration, so the conversion exists in exactly one place.

    Args:
        date_text: An expiry as "YYYY-MM-DD".

    Returns:
        The same date as "yyyyMMdd".

    Raises:
        ContractError: If the text is not in the expected format.
    """
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as error:
        raise ContractError(
            f"Could not read {date_text!r} as a date (expected YYYY-MM-DD)."
        ) from error
    return parsed.strftime("%Y%m%d")


def from_tiger_expiry_format(compact_date_text: str) -> str:
    """Convert "yyyyMMdd" back to "YYYY-MM-DD".

    Args:
        compact_date_text: An expiry as "yyyyMMdd".

    Returns:
        The same date as "YYYY-MM-DD".

    Raises:
        ContractError: If the text is not in the expected format.
    """
    try:
        parsed = datetime.strptime(compact_date_text, "%Y%m%d")
    except ValueError as error:
        raise ContractError(
            f"Could not read {compact_date_text!r} as a date (expected yyyyMMdd)."
        ) from error
    return parsed.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Identifier helpers -- thin wrappers, both directions
# ---------------------------------------------------------------------------


def build_identifier(
    underlying: str,
    expiry_compact: str,
    put_call: str,
    strike: float,
) -> str:
    """Build the 21-character option identifier using the SDK's own helper.

    The identifier is never assembled with string formatting. Its padding and
    price-scaling rules are fiddly, and a hand-built one that is subtly wrong
    looks entirely correct while referring to nothing.

    Args:
        underlying: Underlying symbol, e.g. "AAPL".
        expiry_compact: Expiry as "yyyyMMdd".
        put_call: "CALL" or "PUT".
        strike: Strike price.

    Returns:
        The identifier, e.g. "AAPL  260918C00320000".
    """
    return get_option_identifier(underlying, expiry_compact, put_call, strike)


def parse_identifier(identifier: str) -> tuple[str, str, str, float]:
    """Split a 21-character option identifier back into its four elements.

    DORMANT, not dead. A Phase 3 deliverable -- the spec asks for both
    directions of the identifier conversion -- that nothing calls yet because
    every current caller starts from the four elements rather than the string.
    Anything that reads identifiers back from Tiger will need it.

    The reverse of build_identifier, again using the SDK's helper rather than
    slicing the string by hand.

    Args:
        identifier: An identifier, e.g. "AAPL  260918C00320000".

    Returns:
        A tuple of (underlying, expiry, put_call, strike).

    Raises:
        ContractError: If the identifier cannot be parsed.
    """
    underlying, expiry, put_call, strike = extract_option_info(identifier)

    if underlying is None or expiry is None or put_call is None or strike is None:
        raise ContractError(f"Could not parse the option identifier {identifier!r}.")

    return underlying, expiry, put_call, float(strike)


# ---------------------------------------------------------------------------
# Validation steps
# ---------------------------------------------------------------------------


def validate_option_type(option_type: str) -> str:
    """Check the option type and return it normalised.

    Args:
        option_type: What the caller supplied.

    Returns:
        "CALL" or "PUT".

    Raises:
        ContractError: If it is neither.
    """
    normalised = option_type.strip().upper()
    if normalised not in VALID_OPTION_TYPES:
        raise ContractError(
            f"Option type must be CALL or PUT, not {option_type!r}."
        )
    return normalised


# --------------------------------------------------------------------------
# RESOLVE
# --------------------------------------------------------------------------

#: How many neighbouring strikes to name when a requested one does not exist.
NEAREST_STRIKE_COUNT = 3


@dataclass(frozen=True)
class OptionContractInfo:
    """One verified, tradable option contract.

    Every field here came from Tiger. Nothing is constructed locally except
    the two date formats, which are conversions of a date Tiger supplied.
    """

    identifier: str  # 21-character OCC code, e.g. "AAPL  260918C00320000"
    underlying: str
    expiry_date_text: str  # "YYYY-MM-DD", the format Tiger's expiry list uses
    expiry_compact: str  # "yyyyMMdd", the format Tiger's contract calls want
    strike: float
    put_call: str  # "CALL" or "PUT"
    multiplier: float
    contract_id: int | None
    days_to_expiry: int
    name: str  # underlying's display name from get_contract, e.g. "Apple"

    #: Always None from the API -- see read_min_tick(). Kept on the dataclass so
    #: its absence is visible rather than surprising in Phase 4.
    min_tick: float | None = None

    @property
    def shares_per_contract(self) -> float:
        """Return how many shares one contract controls.

        This is the number that turns a $5.20 premium into $520 of cash. It is
        the single most expensive thing to get wrong, so it is named plainly
        rather than left as "multiplier".
        """
        return self.multiplier

    def describe(self) -> str:
        """Return a one-line human description of the contract."""
        return (
            f"{self.underlying} {self.expiry_date_text} "
            f"{self.strike:,.2f} {self.put_call}"
        )


def resolve_expiry(quote_client, underlying: str, expiry_date_text: str):
    """Find the requested expiry among the ones Tiger lists, and check it.

    Three outcomes, not two. Tiger returns dates that have already passed in
    its expiration list -- 2026-09-02 was still the first entry on 2026-09-03 --
    so "is it listed" and "is it still tradable" are separate questions:

      1. not listed          -> ExpiryNotListedError, naming the real dates
      2. listed but past     -> ExpiredContractError, naming the next tradable
      3. listed and current  -> return it

    Collapsing 2 into 1 would report a real, correctly typed date as though it
    were a typo.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol.
        expiry_date_text: The requested expiry as "YYYY-MM-DD".

    Returns:
        The matching OptionExpiry from market.list_expirations.

    Raises:
        ExpiryNotListedError: If Tiger does not list that date.
        ExpiredContractError: If it is listed but has already passed.
    """
    try:
        expiries = list_expirations(quote_client, underlying)
    except MarketDataError as error:
        raise ContractError(str(error)) from error

    matching_expiry = None
    for expiry in expiries:
        if expiry.date_text == expiry_date_text:
            matching_expiry = expiry
            break

    if matching_expiry is None:
        available_dates = []
        for expiry in expiries[:8]:
            if expiry.days_to_expiry >= 0:
                available_dates.append(expiry.date_text)

        raise ExpiryNotListedError(
            f"{expiry_date_text} is not a listed expiration for {underlying}.\n"
            f"Nearest available: {', '.join(available_dates)}"
        )

    if matching_expiry.days_to_expiry < 0:
        next_tradable = find_next_tradable_expiry(expiries)
        days_past = abs(matching_expiry.days_to_expiry)

        raise ExpiredContractError(
            f"{expiry_date_text} has EXPIRED. It expired {days_past} day(s) ago "
            "and is no longer tradable.\n"
            "Tiger still returns recently expired dates in its expiration list, "
            "which is why this looked available.\n"
            f"Next tradable expiry: {next_tradable}"
        )

    return matching_expiry


def find_next_tradable_expiry(expiries) -> str:
    """Return the soonest expiry that has not yet passed.

    Args:
        expiries: OptionExpiry objects, sorted soonest first.

    Returns:
        The date as "YYYY-MM-DD", or a plain message if none remain.
    """
    for expiry in expiries:
        if expiry.days_to_expiry >= 0:
            return expiry.date_text
    return "(none listed)"


def list_strikes_for_expiry(
    trade_client,
    underlying: str,
    expiry_compact: str,
    put_call: str,
) -> list[float]:
    """List every strike Tiger lists for one underlying, expiry and side.

    Args:
        trade_client: A tigeropen TradeClient.
        underlying: Underlying symbol.
        expiry_compact: Expiry as "yyyyMMdd".
        put_call: "CALL" or "PUT".

    Returns:
        Strikes as floats, sorted ascending.

    Raises:
        ExpiryNotListedError: If Tiger has no contracts at all for that expiry.
    """
    DERIVATIVE_CONTRACTS_LIMITER.wait()

    contracts = trade_client.get_derivative_contracts(
        symbol=underlying,
        sec_type=SecurityType.OPT,
        expiry=expiry_compact,
    )

    # QUIRK: an expiry Tiger does not list returns an EMPTY LIST, not an error.
    # Left unchecked that reads downstream as "this expiry has no strikes",
    # when the truth is "this expiry does not exist". The two need different
    # messages, so emptiness is turned into the right one here.
    if not contracts:
        raise ExpiryNotListedError(
            f"Tiger returned no contracts at all for {underlying} "
            f"expiring {expiry_compact}, so that expiry does not exist."
        )

    strikes = []
    for contract in contracts:
        contract_side = getattr(contract, "put_call", None)
        if contract_side != put_call:
            continue

        raw_strike = getattr(contract, "strike", None)
        if raw_strike is None:
            continue

        # QUIRK: on these ladder objects `strike` is a STRING ('50.0'), while
        # get_contract returns a float (320.0). Sorting the strings would put
        # '100.0' before '95.0', so a "nearest strikes" message built without
        # this conversion would name the wrong strikes while looking correct.
        strikes.append(float(raw_strike))

    strikes.sort()
    return strikes


def identifier_from_ladder_entry(contract) -> str | None:
    """Read the OCC identifier off an entry from get_derivative_contracts.

    Args:
        contract: One Contract from get_derivative_contracts.

    Returns:
        The identifier, or None if the entry does not carry one.
    """
    # QUIRK: `identifier` and `name` swap meaning between the two calls.
    #   get_contract              -> identifier='AAPL  260918C00320000', name='Apple'
    #   get_derivative_contracts  -> identifier=None,  name='AAPL  260918C00050000'
    # Reading `.identifier` off a ladder entry yields None, which then travels
    # onward as a missing contract instead of an obvious mistake. On these
    # objects the OCC code lives in `name`.
    return getattr(contract, "name", None)


def find_nearest_strikes(
    available_strikes: list[float],
    wanted_strike: float,
    count: int = NEAREST_STRIKE_COUNT,
) -> list[float]:
    """Pick the strikes closest to one that does not exist.

    Selection is by distance, not by a symmetric window either side, so a
    strike sitting between two listed ones can return three neighbours below
    and one above. That is the more useful answer: it names the strikes
    actually closest to what was asked for.

    Args:
        available_strikes: Strikes that do exist, sorted ascending.
        wanted_strike: The strike that was asked for.
        count: Half the number to return.

    Returns:
        Up to 2*count strikes, the closest by distance, sorted ascending.
    """
    if not available_strikes:
        return []

    strikes_by_distance = sorted(
        available_strikes,
        key=lambda strike: abs(strike - wanted_strike),
    )
    nearest = strikes_by_distance[: count * 2]
    nearest.sort()
    return nearest


def read_min_tick(contract) -> float | None:
    """Read the minimum price increment from a contract, if it has one.

    Args:
        contract: A Contract from get_contract.

    Returns:
        The tick size, or None.
    """
    # QUIRK: min_tick comes back None from both get_contract and
    # get_derivative_contracts, so there is no API source for the valid price
    # increment. Rather than guess a convention, the limit price is a typed
    # input in Phase 4 -- see scrap/SPEC-ADDENDUM-manual-market-data.md sections 2
    # and 9. This function exists so the absence is explicit rather than
    # something a future reader has to rediscover.
    return getattr(contract, "min_tick", None)


def verify_contract_with_tiger(
    trade_client,
    underlying: str,
    expiry_compact: str,
    strike: float,
    put_call: str,
):
    """Ask Tiger to resolve the contract, and let it refuse.

    This is the authoritative check. A strike that does not exist comes back as
    ERROR 1200 bad_request rather than as a plausible-looking object, so the
    exchange decides what is real instead of any rule written here.

    Args:
        trade_client: A tigeropen TradeClient.
        underlying: Underlying symbol.
        expiry_compact: Expiry as "yyyyMMdd".
        strike: Strike price.
        put_call: "CALL" or "PUT".

    Returns:
        The Contract object, or None if Tiger returned nothing.

    Raises:
        Exception: Whatever the SDK raised. The caller decides what it means,
            because it needs the strike ladder to say so usefully.
    """
    CONTRACT_LIMITER.wait()

    return trade_client.get_contract(
        symbol=underlying,
        sec_type=SecurityType.OPT,
        currency=Currency.USD,
        expiry=expiry_compact,
        strike=strike,
        put_call=put_call,
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def find_option_contract(
    quote_client,
    trade_client,
    underlying: str,
    option_type: str,
    strike: float,
    expiry: str,
) -> OptionContractInfo:
    """Turn a human request into exactly one verified, tradable contract.

    Both clients are needed and they do different jobs: expirations come from
    the QuoteClient, contract identity from the TradeClient. They are passed in
    rather than built here so that this function stays testable and so callers
    keep control of client construction.

    Args:
        quote_client: A tigeropen QuoteClient.
        trade_client: A tigeropen TradeClient.
        underlying: Underlying symbol, e.g. "AAPL".
        option_type: "CALL" or "PUT".
        strike: Strike price, e.g. 320.
        expiry: Expiry as "YYYY-MM-DD", as a human would write it.

    Returns:
        The verified contract's identity and metadata. No market data: that
        arrives later from a MarketDataProvider.

    Raises:
        ContractError: Or one of its subclasses, each with a message naming
            what to do about it.
    """
    normalised_underlying = underlying.strip().upper()
    normalised_type = validate_option_type(option_type)

    matching_expiry = resolve_expiry(quote_client, normalised_underlying, expiry)
    expiry_compact = to_tiger_expiry_format(matching_expiry.date_text)

    try:
        contract = verify_contract_with_tiger(
            trade_client,
            normalised_underlying,
            expiry_compact,
            strike,
            normalised_type,
        )
    except Exception as error:
        raise _explain_contract_failure(
            trade_client,
            normalised_underlying,
            expiry_compact,
            strike,
            normalised_type,
            error,
        ) from error

    if contract is None:
        raise _explain_contract_failure(
            trade_client,
            normalised_underlying,
            expiry_compact,
            strike,
            normalised_type,
            None,
        )

    return _build_contract_info(
        contract,
        normalised_underlying,
        matching_expiry,
        expiry_compact,
        normalised_type,
        strike,
    )


def _explain_contract_failure(
    trade_client,
    underlying: str,
    expiry_compact: str,
    strike: float,
    put_call: str,
    error: Exception | None,
) -> ContractError:
    """Turn a lookup failure into a message that says what to do about it.

    The strike ladder is fetched only now, on the failure path, because it is a
    second API call and there is no reason to spend it when the lookup worked.

    The ladder is also the authority on which of two different things went
    wrong: a strike that is genuinely not listed, or a strike that is listed
    but which Tiger still refused. Those need different messages.

    Args:
        trade_client: A tigeropen TradeClient.
        underlying: Underlying symbol.
        expiry_compact: Expiry as "yyyyMMdd".
        strike: The strike that was asked for.
        put_call: "CALL" or "PUT".
        error: The exception the SDK raised, or None if it returned nothing.

    Returns:
        The exception to raise. Returned rather than raised so the caller can
        chain it to the original with `from`.
    """
    try:
        available_strikes = list_strikes_for_expiry(
            trade_client, underlying, expiry_compact, put_call
        )
    except ContractError as ladder_error:
        return ladder_error

    strike_exists_in_ladder = strike in available_strikes

    if not strike_exists_in_ladder:
        nearest = find_nearest_strikes(available_strikes, strike)
        nearest_text = ", ".join(f"{value:,.2f}" for value in nearest)

        return StrikeNotFoundError(
            f"Strike {strike:,.2f} does not exist for {underlying} "
            f"{from_tiger_expiry_format(expiry_compact)} {put_call}.\n"
            f"Available near {strike:,.2f}: {nearest_text}"
        )

    # The strike is listed, so this is not a bad strike. Something else went
    # wrong and guessing would be worse than saying so.
    detail = f"{type(error).__name__}: {error}" if error else "it returned nothing"
    return ContractError(
        f"Strike {strike:,.2f} IS listed for {underlying} "
        f"{from_tiger_expiry_format(expiry_compact)} {put_call}, but Tiger would "
        f"not resolve the contract: {detail}"
    )


def _build_contract_info(
    contract,
    underlying: str,
    matching_expiry,
    expiry_compact: str,
    put_call: str,
    requested_strike: float,
) -> OptionContractInfo:
    """Assemble the result from a verified Contract object.

    Args:
        contract: The Contract returned by get_contract.
        underlying: Underlying symbol.
        matching_expiry: The OptionExpiry that was resolved.
        expiry_compact: Expiry as "yyyyMMdd".
        put_call: "CALL" or "PUT".
        requested_strike: The strike as the caller asked for it.

    Returns:
        The populated OptionContractInfo.

    Raises:
        ContractError: If the identifier Tiger returned disagrees with the one
            the SDK helper builds from the same four elements.
    """
    # get_contract populates `identifier` (the OCC code) and puts the company
    # name in `name` -- the opposite way round from the ladder entries. See
    # identifier_from_ladder_entry().
    identifier = getattr(contract, "identifier", None)

    resolved_strike = getattr(contract, "strike", None)
    if resolved_strike is None:
        resolved_strike = requested_strike
    resolved_strike = float(resolved_strike)

    expected_identifier = build_identifier(
        underlying, expiry_compact, put_call, resolved_strike
    )

    if identifier is None:
        identifier = expected_identifier
    elif identifier.strip() != expected_identifier.strip():
        # A free cross-check: the identifier Tiger resolved and the one the SDK
        # helper builds from the same four elements must agree. If they ever do
        # not, one of the two is being used wrongly, and continuing would place
        # an order against a contract nobody asked for.
        raise ContractError(
            "Identifier mismatch between Tiger and the SDK helper.\n"
            f"  Tiger returned : {identifier.strip()!r}\n"
            f"  Helper built   : {expected_identifier.strip()!r}\n"
            "Refusing to continue."
        )

    multiplier = getattr(contract, "multiplier", None)
    if multiplier is None:
        multiplier = 100.0
    multiplier = float(multiplier)

    return OptionContractInfo(
        identifier=identifier.strip(),
        underlying=underlying,
        expiry_date_text=matching_expiry.date_text,
        expiry_compact=expiry_compact,
        strike=resolved_strike,
        put_call=put_call,
        multiplier=multiplier,
        contract_id=getattr(contract, "contract_id", None),
        days_to_expiry=days_until_expiry(matching_expiry.expiry_date),
        name=str(getattr(contract, "name", "") or ""),
        min_tick=read_min_tick(contract),
    )


# --------------------------------------------------------------------------
# SELECTION
# --------------------------------------------------------------------------

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
