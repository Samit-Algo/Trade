"""Exception to HTTP status mapping, in one table.

Two ideas govern this module.

**Status codes are coarse; `error_code` is precise.** A client should branch on
the machine-readable `error_code` string, never on prose. Several different
problems share status 400, and a caller that greps the message will break the
first time the wording improves.

**Messages are written for a human reading them at 2am.** Say what went wrong
and what to do about it. `detail` carries the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from tiger_backend.contracts import (
    ContractError,
    ExpiredContractError,
    ExpiryNotListedError,
    StrikeNotFoundError,
)
from tiger_backend.market import MarketDataError
from tiger_backend.orders import BracketError, OrderSubmissionError
from tiger_backend.pricing import PricingError
from tiger_backend.providers import QuoteEntryError
from tiger_backend.safety import LiveTradingBlocked


@dataclass(frozen=True)
class ApiError(Exception):
    """An error with a status, a stable code, and something useful to say."""

    status_code: int
    error_code: str
    message: str
    detail: dict | None = None


#: Exception type -> (status, error_code). Order matters: the most specific
#: subclasses come first, because ExpiredContractError is also a ContractError.
EXCEPTION_MAP: tuple[tuple[type, int, str], ...] = (
    # An expiry that is listed but has passed. 410 Gone says exactly that:
    # it existed, and it does not any more. Kept distinct from 404 so a client
    # can tell "you mistyped a date" from "that contract has expired".
    (ExpiredContractError, 410, "EXPIRY_EXPIRED"),
    (ExpiryNotListedError, 404, "EXPIRY_NOT_LISTED"),
    # The request is well formed; the strike simply does not exist.
    (StrikeNotFoundError, 422, "STRIKE_NOT_FOUND"),
    (BracketError, 422, "BRACKET_INVALID"),
    (PricingError, 422, "PRICING_FAILED"),
    (ContractError, 400, "CONTRACT_INVALID"),
    (QuoteEntryError, 400, "QUOTE_REJECTED"),
    # The safety locks. 403 is the only status an order refusal ever returns.
    (LiveTradingBlocked, 403, "BLOCKED_BY_SAFETY_LOCK"),
    # The broker refused, which is upstream of us.
    (OrderSubmissionError, 502, "UPSTREAM_REJECTED"),
    (MarketDataError, 502, "UPSTREAM_NO_DATA"),
)


def classify_exception(error: Exception) -> ApiError:
    """Turn a library exception into an HTTP-shaped error.

    Args:
        error: Whatever the library raised.

    Returns:
        The ApiError to return to the client.
    """
    message = str(error)

    # A Tiger entitlement refusal is not our bug and not the client's mistake.
    # It is a purchase that has not been made, and it deserves saying so
    # rather than being flattened into a generic upstream failure.
    if "permission denied" in message.lower():
        return ApiError(
            status_code=502,
            error_code="UPSTREAM_PERMISSION_DENIED",
            message=(
                "Tiger refused this data for lack of a market data "
                "entitlement. This is a purchase, not a fault: see "
                "HANDOVER.md section 2."
            ),
            detail={"broker_message": message},
        )

    for exception_type, status_code, error_code in EXCEPTION_MAP:
        if isinstance(error, exception_type):
            return ApiError(
                status_code=status_code,
                error_code=error_code,
                message=message,
            )

    return ApiError(
        status_code=500,
        error_code="INTERNAL_ERROR",
        message=f"{type(error).__name__}: {message}",
    )
