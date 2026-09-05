"""Turning a contract into a string, and back again.

Tiger uses two expiry formats and one 21-character OCC identifier. Getting a
character wrong here means trading a different contract, so every conversion
is in this one file with a test each.
"""

from __future__ import annotations

from datetime import datetime

from tigeropen.common.util.contract_utils import (


    extract_option_info,
    get_option_identifier,
)
from .errors import ContractError

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
