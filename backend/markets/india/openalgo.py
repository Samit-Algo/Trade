"""Reaching OpenAlgo, and asking it whether an order would be real.

OpenAlgo holds the Angel One session and does the broker work; this service
is an HTTP client of that local process. One client object carries both
market data and orders, so the separation that matters -- only one file may
place an order -- is kept by submit.py, not by the client.

THE ONE THING TO KNOW ABOUT PAPER TRADING. Angel has no paper account.
OpenAlgo has one application-wide "Analyze" switch: ON means every order is
simulated, OFF means real orders on the real Angel account -- through the
same key and the same call. That switch lives outside this process and can
be flipped in the browser at any moment, so it is read before EVERY order,
never remembered.
"""

from __future__ import annotations

from backend.core.safety import LiveTradingBlocked
from backend.services.market import MarketDataError

from ..base import MarketNotReady

#: Tags every order this service sends, in OpenAlgo's own books.
STRATEGY = "tiger-options-backend"


class AnalyzerStateUnknown(LiveTradingBlocked):
    """OpenAlgo could not say whether Analyze mode is on. Nothing is sent."""


def build_client(settings):
    """Construct the OpenAlgo client. Does not touch the network.

    Raises:
        MarketNotReady: When OPENALGO_API_KEY is blank.
    """
    if not settings.openalgo_api_key:
        raise MarketNotReady(
            "India is not connected: OPENALGO_API_KEY is blank in "
            "config/india.env. Paste OpenAlgo's own key from its API Key page "
            "and restart the server."
        )

    from openalgo import api

    return api(api_key=settings.openalgo_api_key, host=settings.openalgo_host)


def unwrap(response, what: str):
    """Return what a successful OpenAlgo reply carries, or raise.

    OpenAlgo is not consistent about where it puts a result: the books nest
    it under "data", placeorder puts the order id at the top level. So "data"
    is preferred when present and the whole reply is returned otherwise.

    Args:
        response: Whatever the client returned.
        what: What was being asked, for the message.

    Raises:
        MarketDataError: On anything but a success.
    """
    if not isinstance(response, dict):
        raise MarketDataError(f"{what}: OpenAlgo returned {response!r}.")
    if response.get("status") != "success":
        raise MarketDataError(
            f"{what} failed. OpenAlgo said: {response.get('message', response)}"
        )
    if "data" in response:
        return response["data"]
    return response


def read_analyze_mode(client) -> bool:
    """Ask OpenAlgo whether Analyze (simulated) mode is on, right now.

    Returns:
        True for simulated, False for real orders.

    Raises:
        AnalyzerStateUnknown: When the answer cannot be read. Never a guess:
            guessing wrong in one direction spends real money.
    """
    try:
        response = client.analyzerstatus()
    except Exception as error:  # noqa: BLE001 -- reported the same way
        raise AnalyzerStateUnknown(
            f"Could not ask OpenAlgo whether Analyze mode is on "
            f"({type(error).__name__}: {error}). Nothing was sent. Is OpenAlgo "
            "running?"
        ) from error

    data = response.get("data") if isinstance(response, dict) else None
    mode = (data or {}).get("analyze_mode")
    if not isinstance(mode, bool):
        raise AnalyzerStateUnknown(
            f"OpenAlgo did not report a usable Analyze mode ({response!r}). "
            "Nothing was sent."
        )
    return mode


def resolve_mode(client, allow_live: bool) -> str:
    """PAPER or LIVE: what an order sent now would be.

    Raises:
        AnalyzerStateUnknown: When Analyze mode cannot be read.
        LiveTradingBlocked: When Analyze is OFF and ALLOW_LIVE is false.
    """
    if read_analyze_mode(client):
        return "PAPER"
    if not allow_live:
        raise LiveTradingBlocked(
            "OpenAlgo's Analyze mode is OFF, so this order would be REAL, and "
            "ALLOW_LIVE is false in config/india.env. Nothing was sent. Switch "
            "OpenAlgo to Analyze mode, or set ALLOW_LIVE=true only when you "
            "mean to trade real money."
        )
    return "LIVE"
