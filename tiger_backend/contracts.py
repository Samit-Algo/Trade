"""Phase 3 -- turn a human request into exactly one verified contract.

Contract identity only. Nothing here fetches a price, and nothing here prompts
for one. Bid, ask, volume, open interest and the limit price arrive later from
a MarketDataProvider, as set out in SPEC-ADDENDUM-manual-market-data.md.

That split is why OptionContractInfo carries no market-data fields at all.
Holding them here as None would be worse than not holding them: a `bid` of None
reads as "there is no bid" when the truth is "nobody has asked yet".

The verification chain, in order, and each step has a different authority:

  1. option_type       is CALL or PUT                    -- checked locally
  2. expiry            exists and has not passed         -- Tiger's expiry list
  3. strike            exists for that expiry and side   -- Tiger's strike ladder
  4. contract          resolves to a real tradable thing -- Tiger's own refusal

Step 4 matters most. An impossible strike comes back from get_contract as
ERROR 1200 bad_request, so the final word on whether a contract exists belongs
to the exchange rather than to any check written here.

Documented calls used, all read-only:
  TradeClient.get_contract(symbol, sec_type, currency, exchange, expiry,
                           strike, put_call, lang)                    60/min
  TradeClient.get_derivative_contracts(symbol, sec_type, expiry, lang) 60/min
https://docs-en.itigerup.com/docs/get-contract
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tigeropen.common.consts import Currency, SecurityType
from tigeropen.common.util.contract_utils import (
    extract_option_info,
    get_option_identifier,
)

from .market import MarketDataError, days_until_expiry, list_expirations
from .throttle import CONTRACT_LIMITER, DERIVATIVE_CONTRACTS_LIMITER

VALID_OPTION_TYPES = ("CALL", "PUT")

#: How many neighbouring strikes to name when a requested one does not exist.
NEAREST_STRIKE_COUNT = 3


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
    # input in Phase 4 -- see SPEC-ADDENDUM-manual-market-data.md sections 2
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
