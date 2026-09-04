"""Account, capabilities and expirations. All read-only."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter

from tiger_backend.market import list_expirations
from tiger_backend.positions import SECURITIES_SEGMENT
from tiger_backend.throttle import MANAGED_ACCOUNTS_LIMITER, PRIME_ASSETS_LIMITER

from ..deps import get_quote_client, get_settings, get_trade_client
from ..errors import ApiError
from ..models import (
    AccountResponse,
    CapabilitiesResponse,
    CapabilityRow,
    ExpirationsResponse,
    ExpiryOut,
)

router = APIRouter(tags=["market"])

#: How old a capability probe may be before it is called stale. The underlying
#: facts change only when a market data package is bought, so an hour is
#: generous rather than tight.
CAPABILITY_STALE_AFTER_SECONDS = 3600

_capability_lock = threading.Lock()
_last_probe: dict | None = None


@router.get("/account", response_model=AccountResponse)
def read_account() -> AccountResponse:
    """Report what Tiger says about the configured account.

    Cash available is reported and buying power is reported beside it, but
    nothing in this project costs an order against buying power: on a Reg T
    margin account that figure is roughly four times the cash and the
    difference is borrowed.

    Returns:
        The account payload.
    """
    settings = get_settings()
    trade_client = get_trade_client()

    MANAGED_ACCOUNTS_LIMITER.wait()
    profiles = trade_client.get_managed_accounts()

    matching_profile = None
    for profile in profiles or []:
        if getattr(profile, "account", None) == settings.account:
            matching_profile = profile
            break

    PRIME_ASSETS_LIMITER.wait()
    portfolio = trade_client.get_prime_assets(base_currency="USD")
    segments = getattr(portfolio, "segments", None) or {}
    segment = segments.get(SECURITIES_SEGMENT)

    def read(source, name):
        """Read an optional attribute off an SDK object."""
        if source is None:
            return None
        return getattr(source, name, None)

    return AccountResponse(
        account_masked=settings.masked_account,
        account_type=read(matching_profile, "account_type"),
        status=read(matching_profile, "status"),
        capability=read(matching_profile, "capability"),
        currency=read(segment, "currency"),
        cash_available_for_trade=read(segment, "cash_available_for_trade"),
        buying_power=read(segment, "buying_power"),
        net_liquidation=read(segment, "net_liquidation"),
    )


@router.get("/expirations/{underlying}", response_model=ExpirationsResponse)
def read_expirations(underlying: str) -> ExpirationsResponse:
    """List every expiration date Tiger reports for an underlying.

    Nothing here constructs a date. Tiger returns recently expired dates in
    this list, so each row carries an `expired` flag rather than being
    silently filtered out -- a caller should see what the broker actually said.

    Args:
        underlying: Underlying symbol, e.g. AAPL.

    Returns:
        The expirations payload.
    """
    quote_client = get_quote_client()
    expiries = list_expirations(quote_client, underlying.upper())

    rows = []
    for expiry in expiries:
        rows.append(
            ExpiryOut(
                date=expiry.date_text,
                days_to_expiry=expiry.days_to_expiry,
                period=expiry.period_label,
                expired=expiry.days_to_expiry < 0,
            )
        )

    return ExpirationsResponse(underlying=underlying.upper(), expirations=rows)


def describe_age(seconds: float) -> str:
    """Put an age into words, so a stale probe is not misread as current.

    Args:
        seconds: How old the probe is.

    Returns:
        A short human description.
    """
    if seconds < 90:
        return f"{seconds:.0f} seconds ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes ago"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours ago"
    return f"{hours / 24:.1f} days ago"


def build_capabilities_response(probe: dict) -> CapabilitiesResponse:
    """Wrap a stored probe with its current age.

    Args:
        probe: The stored probe, with captured_at and results.

    Returns:
        The response, including how old the data is.
    """
    now = datetime.now(timezone.utc)
    age_seconds = (now - probe["captured_at"]).total_seconds()

    return CapabilitiesResponse(
        captured_at=probe["captured_at"],
        age_seconds=round(age_seconds, 1),
        age_description=f"probed {describe_age(age_seconds)}",
        is_stale=age_seconds > CAPABILITY_STALE_AFTER_SECONDS,
        results=probe["results"],
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
def read_capabilities() -> CapabilitiesResponse:
    """Return the last capability probe, with its age.

    This does NOT re-probe: a full probe makes twenty live API calls and takes
    around ten seconds, which is not what a GET should do. Use
    POST /capabilities/probe to refresh. The response states its own age
    prominently so a stale answer is not mistaken for a current one.

    Returns:
        The cached probe.

    Raises:
        ApiError: 404 when nothing has been probed since the service started.
    """
    with _capability_lock:
        probe = _last_probe

    if probe is None:
        raise ApiError(
            status_code=404,
            error_code="NO_CAPABILITY_PROBE",
            message=(
                "No capability probe has been run since this service started. "
                "POST to /capabilities/probe to run one; it takes about ten "
                "seconds and makes twenty live API calls."
            ),
        )

    return build_capabilities_response(probe)


@router.post("/capabilities/probe", response_model=CapabilitiesResponse)
def run_capability_probe() -> CapabilitiesResponse:
    """Re-run the endpoint probe and cache the result.

    Read-only against Tiger despite being a POST: it calls each endpoint once
    with a harmless argument and places no orders. It is a POST because it is
    slow and has a side effect on this service's cache, not because it changes
    anything at the broker.

    Returns:
        The fresh probe.
    """
    global _last_probe

    # Imported here rather than at module scope: the probe script lives in
    # scripts/, which is not a package, so it is loaded by path on demand.
    from ..probe import run_probe

    results = run_probe(get_trade_client(), get_quote_client())
    probe = {"captured_at": datetime.now(timezone.utc), "results": results}

    with _capability_lock:
        _last_probe = probe

    return build_capabilities_response(probe)
