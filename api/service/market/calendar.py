"""What days matter: expiries, and the maths on dates.

Every date in this project is a US market date. Reading an expiry timestamp as
UTC slips it to the wrong day, so the conversion lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from tigeropen.common.consts import Market

from ..core.broker import EXPIRATIONS_LIMITER
from .fields import MarketDataError, _read_optional_int, _read_text


#: US options trade on US Eastern time. Days-to-expiry must be counted on the
#: market's calendar, not on the clock of whoever is running this (IST here).
MARKET_TIMEZONE = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Data shapes
#
# The script that prints tables works with these, never with pandas. Keeping
# pandas inside this module means there is exactly one place to look when a
# column name changes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionExpiry:
    """One expiration date that Tiger says exists for an underlying."""

    date_text: str  # "YYYY-MM-DD", exactly as Tiger returned it
    expiry_date: date
    timestamp_ms: int
    period_tag: str  # "m" for monthly, "w" for weekly, "" if not reported
    days_to_expiry: int
    option_symbol: str  # Usually the underlying; index options can differ

    @property
    def period_label(self) -> str:
        """Return a readable version of the monthly/weekly tag."""
        if self.period_tag == "m":
            return "monthly"
        if self.period_tag == "w":
            return "weekly"
        return "unknown"


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------


def today_in_market_timezone() -> date:
    """Return today's date on the US market's clock.

    Returns:
        The current date in US/Eastern.
    """
    now_in_new_york = datetime.now(MARKET_TIMEZONE)
    return now_in_new_york.date()


def milliseconds_to_date(milliseconds: int) -> date:
    """Convert one of Tiger's millisecond timestamps to a calendar date.

    Args:
        milliseconds: Milliseconds since 1970-01-01 UTC, as Tiger sends them.

    Returns:
        The corresponding date in US/Eastern.
    """
    # Tiger sends milliseconds; Python's fromtimestamp expects seconds.
    seconds = milliseconds / 1000

    moment_in_utc = datetime.fromtimestamp(seconds, tz=timezone.utc)

    # Expiry timestamps are midnight US/Eastern. Reading them in any other
    # zone can land on the previous or next day and shift the whole expiry.
    moment_in_new_york = moment_in_utc.astimezone(MARKET_TIMEZONE)
    return moment_in_new_york.date()


def parse_expiry_date(date_text: str) -> date:
    """Turn Tiger's "YYYY-MM-DD" expiry string into a date object.

    Args:
        date_text: An expiry date string exactly as the API returned it.

    Returns:
        The parsed date.

    Raises:
        MarketDataError: If the text is not in the expected format.
    """
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as error:
        raise MarketDataError(
            f"Could not read {date_text!r} as an expiry date (expected YYYY-MM-DD)."
        ) from error
    return parsed.date()


def days_until_expiry(expiry_date: date) -> int:
    """Count whole days from today to an expiry date.

    Args:
        expiry_date: The date the option expires.

    Returns:
        Days remaining. 0 means it expires today; a negative number means the
        date has already passed.
    """
    today = today_in_market_timezone()
    difference = expiry_date - today
    return difference.days


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------


def list_expirations(quote_client, underlying: str) -> list[OptionExpiry]:
    """List every expiration date Tiger reports for an underlying.

    This is the only source of expiry dates in the project. Nothing anywhere
    builds a date from a calendar rule, because listed expiries are irregular
    and a constructed date that does not exist fails only at order time.

    Args:
        quote_client: A tigeropen QuoteClient.
        underlying: Underlying symbol, for example "AAPL".

    Returns:
        Expiries sorted by date, soonest first.

    Raises:
        MarketDataError: If Tiger returns no expirations for the symbol.
    """
    EXPIRATIONS_LIMITER.wait()

    # market is passed explicitly rather than relying on the server default,
    # so a symbol that could be read as non-US cannot silently change market.
    expirations_frame = quote_client.get_option_expirations(
        symbols=[underlying],
        market=Market.US,
    )

    if expirations_frame is None or expirations_frame.empty:
        raise MarketDataError(
            f"Tiger returned no option expirations for {underlying!r}. "
            "Check the symbol, and that it has listed options."
        )

    expiries = []
    for _index, row in expirations_frame.iterrows():
        date_text = _read_text(row, "date")
        if not date_text:
            continue

        expiry_date = parse_expiry_date(date_text)
        timestamp_ms = _read_optional_int(row, "timestamp")
        period_tag = _read_text(row, "period_tag")
        option_symbol = _read_text(row, "option_symbol", default=underlying)
        days_remaining = days_until_expiry(expiry_date)

        expiries.append(
            OptionExpiry(
                date_text=date_text,
                expiry_date=expiry_date,
                timestamp_ms=timestamp_ms if timestamp_ms is not None else 0,
                period_tag=period_tag,
                days_to_expiry=days_remaining,
                option_symbol=option_symbol,
            )
        )

    if not expiries:
        raise MarketDataError(
            f"Tiger returned expiration rows for {underlying!r} but none had a date."
        )

    expiries.sort(key=lambda expiry: expiry.expiry_date)
    return expiries
