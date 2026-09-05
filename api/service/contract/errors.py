"""Everything that can go wrong while resolving a contract.

Four failures with four different messages. The distinction that matters most
is the last one: an EXPIRED contract is not a MISSING one. Saying "not found"
about something that existed last week sends you looking in the wrong place,
which is why they are separate types and the HTTP layer gives them separate
status codes.
"""

from __future__ import annotations

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
