"""Which exact Indian option contract are we talking about?

OpenAlgo names an option by its underlying, expiry, strike and side run
together -- "NIFTY30SEP2525150CE" -- and its symbol master says whether that
contract exists, what its lot is, and what its tick is. Nothing here is
constructed without that check: a contract name built from arithmetic alone
fails only at order time, which is the worst time to learn it.

The rules are the US ones, so the page means the same thing in both markets:

    expiry   TRADE_EXPIRY_DATE if set, else the soonest listed expiry at
             least MIN_DAYS_TO_EXPIRY away
    strike   TRADE_STRIKES_OUT whole strikes out of the money; the first is
             the first strike strictly past the underlying's price. In the
             money is never chosen.

             Then, from there, the next STRIKE_CHOICES round strikes are
             compared on today's VOLUME and the busiest is traded: a busy
             contract fills and exits at fair prices, a quiet one may not.
             A tie goes to the nearer strike. When no volume can be read the
             nearest is taken, and the reason says so.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime

from backend.services.contract import (
    ExpiredContractError,
    ExpiryNotListedError,
    OptionContractInfo,
    StrikeNotFoundError,
    SymbolNotListedError,
)
from backend.services.market import MarketDataError, OptionExpiry

from .openalgo import unwrap

#: Where each F&O exchange's underlying INDEX is quoted.
INDEX_EXCHANGE = {"NFO": "NSE_INDEX", "BFO": "BSE_INDEX"}

#: The strike grid, in index points. NIFTY lists every 50.
STRIKE_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
    "BANKEX": 100,
}

#: "NIFTY" "30SEP25" "25150" "CE"
_IDENTIFIER = re.compile(r"^([A-Z&]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$")

SIDE_OF = {"CALL": "CE", "PUT": "PE"}
PUT_CALL_OF = {"CE": "CALL", "PE": "PUT"}


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def compact_expiry(day: date) -> str:
    """30 Sep 2026 -> "30SEP26", the form contract names use."""
    return day.strftime("%d%b%y").upper()


def parse_listed_expiry(text: str) -> date:
    """"30-SEP-26" -- how OpenAlgo's expiry list spells a date -- to a date."""
    return datetime.strptime(text.strip().title(), "%d-%b-%y").date()


def strike_text(strike: float) -> str:
    """25150.0 -> "25150", 25150.5 -> "25150.5"."""
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"


def build_identifier(underlying: str, expiry: date, strike: float, put_call: str) -> str:
    """The contract's OpenAlgo name, e.g. "NIFTY30SEP2625150CE"."""
    return (
        f"{underlying.strip().upper()}{compact_expiry(expiry)}"
        f"{strike_text(strike)}{SIDE_OF[put_call.upper()]}"
    )


def parse_identifier(identifier: str) -> tuple[str, str, str, float]:
    """Read a contract name: (underlying, YYYY-MM-DD, CALL/PUT, strike).

    Raises:
        ValueError: For a name that is not an Indian option.
    """
    match = _IDENTIFIER.match(identifier.strip().upper())
    if not match:
        raise ValueError(f"{identifier!r} is not an Indian option contract name.")
    underlying, expiry_text, strike, side = match.groups()
    expiry = datetime.strptime(expiry_text.title(), "%d%b%y").date()
    return underlying, expiry.isoformat(), PUT_CALL_OF[side], float(strike)


# ---------------------------------------------------------------------------
# Expiries
# ---------------------------------------------------------------------------


def list_expirations(client, underlying: str, exchange: str, today: date) -> list[OptionExpiry]:
    """Every listed option expiry for an underlying, soonest first.

    Raises:
        SymbolNotListedError: When nothing at all is listed.
    """
    data = unwrap(
        client.expiry(symbol=underlying, exchange=exchange, instrumenttype="options"),
        f"The expiry list for {underlying}",
    )
    rows = data if isinstance(data, list) else []

    expiries = []
    for text in rows:
        try:
            day = parse_listed_expiry(str(text))
        except ValueError:
            continue
        expiries.append(
            OptionExpiry(
                date_text=day.isoformat(),
                expiry_date=day,
                timestamp_ms=int(datetime(day.year, day.month, day.day).timestamp() * 1000),
                period_tag="",
                days_to_expiry=(day - today).days,
                option_symbol=underlying,
            )
        )

    if not expiries:
        raise SymbolNotListedError(
            f"OpenAlgo lists no options for {underlying} on {exchange}. Check "
            "the symbol in TRADE_SYMBOLS, and that OpenAlgo's master contract "
            "has been downloaded today."
        )
    return sorted(expiries, key=lambda e: e.expiry_date)


def choose_expiry(expiries: list[OptionExpiry], minimum_days: int, explicit: str | None):
    """Pick the expiry to trade, and say why.

    Returns:
        (the OptionExpiry, a sentence explaining the choice).

    Raises:
        ExpiredContractError: A named expiry has passed.
        ExpiryNotListedError: A named expiry is not listed, or none is far enough.
    """
    listed = ", ".join(e.date_text for e in expiries if e.days_to_expiry >= 0)

    if explicit:
        for expiry in expiries:
            if expiry.date_text == explicit:
                if expiry.days_to_expiry < 0:
                    raise ExpiredContractError(
                        f"The expiry {explicit} has already passed. Listed: {listed}."
                    )
                return expiry, (
                    f"{explicit} was named explicitly ({expiry.days_to_expiry} "
                    "days away)."
                )
        raise ExpiryNotListedError(f"{explicit} is not a listed expiry. Listed: {listed}.")

    for expiry in expiries:
        if expiry.days_to_expiry >= minimum_days:
            return expiry, (
                f"{expiry.date_text} is the soonest expiry at least {minimum_days} "
                f"days away ({expiry.days_to_expiry} days)."
            )
    raise ExpiryNotListedError(
        f"No listed expiry is at least {minimum_days} days away. Listed: {listed}."
    )


# ---------------------------------------------------------------------------
# Strikes
# ---------------------------------------------------------------------------


def otm_strike(price: float, put_call: str, step: float, strikes_out: int) -> float:
    """The strike `strikes_out` whole steps out of the money.

    The first is the first strike STRICTLY past the price: with NIFTY at
    25,150 exactly, the first call is 25,200, never the at-the-money 25,150.
    """
    if put_call.upper() == "CALL":
        first = math.floor(price / step) * step + step
        return first + (strikes_out - 1) * step
    first = math.ceil(price / step) * step - step
    return first - (strikes_out - 1) * step


def describe_contract(client, identifier: str, exchange: str, today: date) -> OptionContractInfo:
    """Confirm a contract exists, and read its lot and tick from the master.

    Raises:
        StrikeNotFoundError: When the symbol master has no such contract.
    """
    try:
        found = unwrap(
            client.symbol(symbol=identifier, exchange=exchange),
            f"The symbol lookup for {identifier}",
        )
    except MarketDataError as error:
        raise StrikeNotFoundError(
            f"{identifier} is not in OpenAlgo's symbol master: {error}"
        ) from error

    if not isinstance(found, dict) or not found.get("symbol"):
        raise StrikeNotFoundError(f"{identifier} is not in OpenAlgo's symbol master.")

    lot_size = int(float(found.get("lotsize") or 0))
    if lot_size <= 0:
        raise MarketDataError(f"OpenAlgo reported no lot size for {identifier}.")

    underlying, expiry_text, put_call, strike = parse_identifier(identifier)
    expiry = date.fromisoformat(expiry_text)
    tick = float(found.get("tick_size") or 0) or None

    return OptionContractInfo(
        identifier=identifier,
        underlying=underlying,
        expiry_date_text=expiry_text,
        expiry_compact=compact_expiry(expiry),
        strike=float(found.get("strike") or strike),
        put_call=put_call,
        # Cash is premium x multiplier x quantity, and quantity counts LOTS
        # here, so a lot is what one "contract" is.
        multiplier=float(lot_size),
        contract_id=int(found["token"]) if str(found.get("token", "")).isdigit() else None,
        days_to_expiry=(expiry - today).days,
        name=str(found.get("name") or underlying),
        min_tick=tick,
    )


def select_contract(
    client, underlying: str, option_type: str, underlying_price: float, *,
    exchange: str, today: date, minimum_days: int, expiry_date_text: str | None,
    strikes_out: int, strike_step: int | None = None, strike_choices: int = 1,
):
    """Choose and verify the contract to trade.

    `strike_step` is the grid strikes are chosen on -- STRIKE_STEP, 100 for
    round NIFTY strikes. Without it the exchange's own grid is used.

    `strike_choices` is how many round strikes, counted outward from
    TRADE_STRIKES_OUT, are compared on today's volume. 1 compares nothing.

    Returns:
        (OptionContractInfo, why this expiry, why this strike).
    """
    wanted = underlying.strip().upper()
    put_call = option_type.strip().upper()
    if put_call not in SIDE_OF:
        raise StrikeNotFoundError(f"Option type must be CALL or PUT (got {option_type!r}).")

    expiries = list_expirations(client, wanted, exchange, today)
    expiry, expiry_reason = choose_expiry(expiries, minimum_days, expiry_date_text)

    step = strike_step or STRIKE_STEPS.get(wanted, 50)
    candidates = [
        otm_strike(underlying_price, put_call, step, strikes_out + extra)
        for extra in range(max(1, strike_choices))
    ]
    strike, volume_note = busiest_strike(
        client, wanted, expiry.expiry_date, candidates, put_call, exchange)
    identifier = build_identifier(wanted, expiry.expiry_date, strike, put_call)
    contract = describe_contract(client, identifier, exchange, today)

    direction = "above" if put_call == "CALL" else "below"
    strike_reason = (
        f"{strike_text(strike)} is {candidates.index(strike) + strikes_out} "
        f"strike(s) of {step} {direction} {wanted} at {underlying_price:,.2f} "
        f"-- out of the money.{volume_note}"
    )
    return contract, expiry_reason, strike_reason


def busiest_strike(client, underlying: str, expiry: date, strikes: list[float],
                   put_call: str, exchange: str) -> tuple[float, str]:
    """Of these strikes, the one with the most volume today, and a sentence why.

    A strike whose quote cannot be read is skipped. With one strike, or none
    readable, the nearest is returned -- never a guess.
    """
    if len(strikes) == 1:
        return strikes[0], ""

    volumes = []
    for strike in strikes:
        symbol = build_identifier(underlying, expiry, strike, put_call)
        try:
            data = unwrap(client.quotes(symbol=symbol, exchange=exchange),
                          f"The quote for {symbol}")
            volumes.append((strike, int(float((data or {}).get("volume") or 0))))
        except Exception:  # noqa: BLE001 -- an unreadable strike is not chosen
            continue

    listing = ", ".join(f"{strike_text(s)}: {v:,}" for s, v in volumes)
    if not volumes or max(v for _, v in volumes) <= 0:
        return strikes[0], (
            f" No volume could be read for {', '.join(strike_text(s) for s in strikes)}"
            f"{' (' + listing + ')' if listing else ''}, so the nearest was taken.")

    # max() keeps the FIRST of equals, and the list runs nearest first.
    best, most = max(volumes, key=lambda item: item[1])
    return best, (
        f" Chosen for volume: the busiest of {len(strikes)} round strikes "
        f"today ({listing}).")
