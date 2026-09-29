"""A contract, and the ways resolving one can fail. Neutral: no broker here.

Each market resolves and selects contracts its own way -- the US one in
backend/markets/us/contract.py -- and answers with an OptionContractInfo, or
raises one of the ContractErrors below. The routes know only these.
"""

from __future__ import annotations

from dataclasses import dataclass


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
