"""Which exact contract are we talking about?

    errors.py       The four ways resolution fails
    identifiers.py  Turning a contract into a string, and back
    resolve.py      Does it exist? Is it expired? What is its ID?

Identity only. Resolving a contract needs no market-data entitlement, which is
what lets the rest of this project work without one.
"""

from __future__ import annotations

from .errors import (
    SymbolNotListedError,
    ContractError, ExpiryNotListedError, ExpiredContractError,
    StrikeNotFoundError
)
from .identifiers import (
    VALID_OPTION_TYPES, to_tiger_expiry_format, from_tiger_expiry_format,
    build_identifier, parse_identifier, validate_option_type
)
from .selection import (
    DEFAULT_MIN_DAYS_TO_EXPIRY, choose_expiry, find_closest_strike,
    select_contract,
)
from .resolve import (
    NEAREST_STRIKE_COUNT, OptionContractInfo, resolve_expiry,
    find_next_tradable_expiry, list_strikes_for_expiry,
    identifier_from_ladder_entry, find_nearest_strikes, read_min_tick,
    verify_contract_with_tiger, find_option_contract
)

__all__ = [
    "SymbolNotListedError", "select_contract",
    "DEFAULT_MIN_DAYS_TO_EXPIRY", "choose_expiry", "find_closest_strike",
    "ContractError", "ExpiryNotListedError", "ExpiredContractError",
    "StrikeNotFoundError", "VALID_OPTION_TYPES", "to_tiger_expiry_format",
    "from_tiger_expiry_format", "build_identifier", "parse_identifier",
    "validate_option_type", "NEAREST_STRIKE_COUNT", "OptionContractInfo",
    "resolve_expiry", "find_next_tradable_expiry",
    "list_strikes_for_expiry", "identifier_from_ladder_entry",
    "find_nearest_strikes", "read_min_tick", "verify_contract_with_tiger",
    "find_option_contract"
]
