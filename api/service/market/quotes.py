"""Where bid, ask, volume and open interest come from.

Implements SPEC-ADDENDUM-manual-market-data.md.

This account cannot fetch option quotes, so they are read off the Tiger app and
typed in. That is a temporary substitution behind a stable seam: everything
that needs a quote takes a MarketDataProvider, and this is the only module that
knows a concrete provider exists.

**The review rule.** If any file other than this one imports
ManualEntryProvider or TigerQuoteProvider by name, the seam has leaked. One
grep is the whole test of whether the design is holding.

Labelling is structural, not a convention. QuoteSnapshot requires `source` and
`captured_at` with no defaults, so a quote cannot be built without saying where
it came from and when. Nothing has to remember to add a label, because nothing
can construct an unlabelled quote in the first place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum


from .prices import calculate_spread

#: How old a typed quote may be, in seconds, before it must be re-entered.
#: You read the app, type five numbers, think, confirm -- and the market has
#: moved. Fetched data does not have this problem; typed data does.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60

#: A typed price this far from the last traded price is treated as a decimal
#: slip until proven otherwise.
DECIMAL_SLIP_HIGH_RATIO = 3.0
DECIMAL_SLIP_LOW_RATIO = 0.33

#: A spread wider than this fraction of the ask asks for confirmation.
WIDE_SPREAD_FRACTION = 0.50


class QuoteSource(Enum):
    """Where a quote came from. Recorded on every snapshot."""

    MANUAL = "MANUAL"
    TIGER_API = "TIGER_API"

    @property
    def tag(self) -> str:
        """Return the short label printed beside prices."""
        if self is QuoteSource.MANUAL:
            return "[MANUAL]"
        return "[TIGER]"


class QuoteEntryError(Exception):
    """Quote entry was abandoned, or could not be completed."""


@dataclass(frozen=True)
class QuoteSnapshot:
    """Market data for one contract, with its provenance attached.

    Named QuoteSnapshot rather than ContractQuote because market.py already
    uses that name for the get_option_briefs response. Two types meaning almost
    the same thing under one name is exactly the confusion this file exists to
    prevent.

    `source` and `captured_at` have no defaults on purpose. See the module
    docstring.
    """

    bid: float
    ask: float
    volume: int | None
    limit_price: float | None
    source: QuoteSource
    captured_at: datetime

    # What the decimal-slip check saw, kept for the audit trail so a bad fill
    # can be diagnosed later. None when the check could not run.
    last_close: float | None = None
    last_close_date: date | None = None
    last_close_ratio: float | None = None

    @property
    def is_manual(self) -> bool:
        """True when these numbers were typed by a human."""
        return self.source is QuoteSource.MANUAL

    @property
    def source_tag(self) -> str:
        """Return the label printed beside prices, e.g. "[MANUAL]"."""
        return self.source.tag

    def age_seconds(self, now: datetime | None = None) -> float:
        """Return how old this quote is.

        Args:
            now: The current time, or None to use the clock.

        Returns:
            Age in seconds.
        """
        current_time = now if now is not None else datetime.now(timezone.utc)
        return (current_time - self.captured_at).total_seconds()

    def is_stale(
        self,
        max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """Decide whether this quote is too old to act on.

        Args:
            max_age_seconds: The limit.
            now: The current time, or None to use the clock.

        Returns:
            True when the quote should be re-entered.
        """
        return self.age_seconds(now) > max_age_seconds


@dataclass(frozen=True)
class BidSnapshot:
    """Just the bid for one contract, with its provenance attached.

    Separate from QuoteSnapshot because valuing a position needs one number,
    not five. Asking for an ask, a volume and a limit price to value something
    you are not trading would be five chances to mistype instead of one.

    `source` and `captured_at` are required here for the same reason they are
    on QuoteSnapshot: an unlabelled price cannot be constructed.
    """

    bid: float
    source: QuoteSource
    captured_at: datetime

    last_close: float | None = None
    last_close_date: date | None = None
    last_close_ratio: float | None = None

    @property
    def is_manual(self) -> bool:
        """True when this number was typed by a human."""
        return self.source is QuoteSource.MANUAL

    @property
    def source_tag(self) -> str:
        """Return the label printed beside the price."""
        return self.source.tag

    def age_seconds(self, now: datetime | None = None) -> float:
        """Return how old this reading is, in seconds."""
        current_time = now if now is not None else datetime.now(timezone.utc)
        return (current_time - self.captured_at).total_seconds()

    def is_stale(
        self,
        max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """Decide whether this reading is too old to value a position with."""
        return self.age_seconds(now) > max_age_seconds


class MarketDataProvider(ABC):
    """Supplies market data for one contract.

    One method. ManualEntryProvider implements it by asking; a future
    TigerQuoteProvider will implement it by fetching, and nothing outside this
    module will need to change.
    """

    @abstractmethod
    def get_quote(self, contract) -> QuoteSnapshot:
        """Return market data for one contract.

        Args:
            contract: An OptionContractInfo from contracts.find_option_contract.

        Returns:
            The quote.

        Raises:
            QuoteEntryError: If a quote could not be obtained.
        """
        raise NotImplementedError

    def get_bid(self, contract) -> BidSnapshot:
        """Return just the bid, for valuing a position rather than trading.

        The default delegates to get_quote, so a provider that fetches
        everything at once needs no extra work. ManualEntryProvider overrides
        it to ask for one number instead of five.

        Args:
            contract: An OptionContractInfo.

        Returns:
            The bid, with its provenance.
        """
        quote = self.get_quote(contract)
        return BidSnapshot(
            bid=quote.bid,
            source=quote.source,
            captured_at=quote.captured_at,
            last_close=quote.last_close,
            last_close_date=quote.last_close_date,
            last_close_ratio=quote.last_close_ratio,
        )


# ---------------------------------------------------------------------------
# Pure validation. No prompting, no I/O -- all of this is unit-testable.
# ---------------------------------------------------------------------------


def check_price_is_positive(value: float, label: str) -> str | None:
    """Check a price is above zero.

    Args:
        value: The price.
        label: What to call it in the message.

    Returns:
        A problem message, or None if the price is fine.
    """
    if value <= 0:
        return f"{label} must be greater than zero (got {value})."
    return None


def check_bid_below_ask(bid: float, ask: float) -> str | None:
    """Check the bid is below the ask.

    A bid above the ask is not a market, it is a typo. The two numbers would
    have been read off adjacent columns and swapped.

    Args:
        bid: Highest price a buyer is offering.
        ask: Lowest price a seller is asking.

    Returns:
        A problem message, or None.
    """
    if bid > ask:
        return (
            f"Bid {bid} is above ask {ask}. That is not a real market -- "
            "the two were probably swapped."
        )
    return None


def is_spread_suspiciously_wide(
    bid: float,
    ask: float,
    fraction: float = WIDE_SPREAD_FRACTION,
) -> bool:
    """Decide whether the spread is wide enough to be worth querying.

    Args:
        bid: The bid.
        ask: The ask.
        fraction: Spread-to-ask fraction above which to ask for confirmation.

    Returns:
        True when the spread looks wrong.
    """
    _spread, spread_percent = calculate_spread(bid, ask)
    if spread_percent is None:
        return False
    return spread_percent > (fraction * 100)


def is_limit_outside_spread(limit_price: float, bid: float, ask: float) -> bool:
    """Decide whether a limit price sits outside the quoted market.

    Legal, and occasionally deliberate, but usually a mistake.

    Args:
        limit_price: The typed limit price.
        bid: The bid.
        ask: The ask.

    Returns:
        True when the limit is outside [bid, ask].
    """
    return limit_price < bid or limit_price > ask


def decimal_slip_ratio(typed_price: float, last_close: float | None) -> float | None:
    """Compare a typed price to the last price the contract actually traded at.

    Args:
        typed_price: What the human typed.
        last_close: The most recent daily close, or None if unavailable.

    Returns:
        The ratio, or None when it cannot be computed.
    """
    if last_close is None or last_close <= 0:
        return None
    return typed_price / last_close


def is_decimal_slip(ratio: float | None) -> bool:
    """Decide whether a ratio looks like a misplaced decimal point.

    This is the single most expensive typing error: it turns $520 into $5,200
    at 100 shares per contract. It is treated as its own failure mode rather
    than one confirmation among several.

    Args:
        ratio: The typed price divided by the last traded close.

    Returns:
        True when the typed price is far enough away to demand an override.
    """
    if ratio is None:
        return False
    return ratio > DECIMAL_SLIP_HIGH_RATIO or ratio < DECIMAL_SLIP_LOW_RATIO


def build_override_phrase(value: float) -> str:
    """Build the phrase that confirms a suspected decimal slip.

    The value is retyped inside the phrase so a reflexive "y" cannot clear it.
    Confirming a wrong number requires writing the wrong number out again.

    Args:
        value: The price the human typed.

    Returns:
        The exact phrase to require, e.g. "USE 52.00".
    """
    return f"USE {value:.2f}"


# ---------------------------------------------------------------------------
# Manual entry
# ---------------------------------------------------------------------------


class ManualEntryProvider(MarketDataProvider):
    """Asks the human for the five numbers the API will not give us.

    Reads bid, ask, volume, open interest and the limit price off the Tiger
    app. Implied volatility is deliberately not asked for: it costs nothing to
    the arithmetic and would be a fifth number to transcribe every time.
    """

    def __init__(self, quote_client=None, input_function=input, output_function=print):
        """Create the provider.

        Args:
            quote_client: A tigeropen QuoteClient, used only for the
                decimal-slip check. None disables that check, and the
                disabling is announced rather than silent.
            input_function: How to read a line. Injectable for testing.
            output_function: How to write a line. Injectable for testing.
        """
        self.quote_client = quote_client
        self.input_function = input_function
        self.output_function = output_function

    def get_quote(self, contract) -> QuoteSnapshot:
        """Prompt for market data for one contract.

        Args:
            contract: An OptionContractInfo.

        Returns:
            The typed quote, stamped MANUAL and timestamped.

        Raises:
            QuoteEntryError: If entry is abandoned.
        """
        self._print_entry_header(contract)

        last_trade = self._fetch_last_traded_close(contract)

        bid = self._prompt_price("Bid")
        ask = self._prompt_price("Ask", other_side_bid=bid)

        # The decimal check runs BEFORE the spread check, deliberately. A
        # misplaced decimal point also produces an absurd spread, so asking
        # about the spread first would report the symptom and swallow the
        # answer to the cause.
        self._check_for_decimal_slip(ask, last_trade)
        self._confirm_wide_spread(bid, ask)

        volume = self._prompt_count("Volume")
        limit_price = self._prompt_limit_price(bid, ask)

        ratio = decimal_slip_ratio(ask, last_trade.close if last_trade else None)

        return QuoteSnapshot(
            bid=bid,
            ask=ask,
            volume=volume,
            limit_price=limit_price,
            source=QuoteSource.MANUAL,
            captured_at=datetime.now(timezone.utc),
            last_close=last_trade.close if last_trade else None,
            last_close_date=last_trade.trade_date if last_trade else None,
            last_close_ratio=ratio,
        )

    def get_bid(self, contract) -> BidSnapshot:
        """Ask the human for just the bid, for valuing a held position.

        One number, not five. A position is worth what someone will pay for
        it, so the bid is the only price that answers the question.

        The decimal-slip check still applies: a mistyped bid produces a
        profit-and-loss figure that is wrong by a factor of ten.

        Args:
            contract: An OptionContractInfo.

        Returns:
            The typed bid, stamped MANUAL and timestamped.

        Raises:
            QuoteEntryError: If entry is abandoned or an override fails.
        """
        self.output_function("")
        self.output_function("-" * 60)
        self.output_function(f"  ENTER THE BID BY HAND  {QuoteSource.MANUAL.tag}")
        self.output_function("-" * 60)
        self.output_function(f"  Contract : {contract.identifier}")
        self.output_function(f"  Read     : {contract.describe()}")
        self.output_function("")
        self.output_function("  A position is worth what someone will PAY for it,")
        self.output_function("  so read the BID, not the ask and not the last price.")
        self.output_function("-" * 60)

        last_trade = self._fetch_last_traded_close(contract)

        bid = self._prompt_price("Bid")
        self._check_for_decimal_slip(bid, last_trade)

        ratio = decimal_slip_ratio(bid, last_trade.close if last_trade else None)

        return BidSnapshot(
            bid=bid,
            source=QuoteSource.MANUAL,
            captured_at=datetime.now(timezone.utc),
            last_close=last_trade.close if last_trade else None,
            last_close_date=last_trade.trade_date if last_trade else None,
            last_close_ratio=ratio,
        )

    def _print_entry_header(self, contract) -> None:
        """Print what the human is being asked to look up."""
        self.output_function("")
        self.output_function("-" * 60)
        self.output_function(f"  ENTER MARKET DATA BY HAND  {QuoteSource.MANUAL.tag}")
        self.output_function("-" * 60)
        self.output_function(f"  Contract : {contract.identifier}")
        self.output_function(f"  Read     : {contract.describe()}")
        self.output_function("")
        self.output_function("  Open this contract in the Tiger app and read the")
        self.output_function("  five values below off the screen.")
        self.output_function("-" * 60)

    def _fetch_last_traded_close(self, contract):
        """Fetch the last daily close, for the decimal-slip check.

        Returns:
            A LastTrade, or None when unavailable.
        """
        if self.quote_client is None:
            self.output_function(
                "  NOTE: decimal-slip check SKIPPED (no quote client supplied)."
            )
            return None

        from .prices import fetch_last_traded_close

        last_trade = fetch_last_traded_close(self.quote_client, contract.identifier)

        if last_trade is None:
            # Announced, never silent. Saying nothing would imply it passed.
            self.output_function(
                "  NOTE: decimal-slip check SKIPPED -- this contract has no "
                "price history to compare against."
            )
            return None

        self.output_function(
            f"  Last traded close: {last_trade.close:,.2f} on "
            f"{last_trade.trade_date} ({last_trade.days_old} days ago)"
        )
        return last_trade

    def _read_line(self, prompt: str) -> str:
        """Read one line, treating an abandoned prompt as an abort."""
        try:
            return self.input_function(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise QuoteEntryError("Quote entry abandoned.") from None

    def _prompt_price(self, label: str, other_side_bid: float | None = None) -> float:
        """Prompt for a price until a valid one is given."""
        while True:
            typed = self._read_line(f"  {label:<16}: ")
            try:
                value = float(typed)
            except ValueError:
                self.output_function(f"    Not a number: {typed!r}")
                continue

            problem = check_price_is_positive(value, label)
            if problem:
                self.output_function(f"    {problem}")
                continue

            if other_side_bid is not None:
                problem = check_bid_below_ask(other_side_bid, value)
                if problem:
                    self.output_function(f"    {problem}")
                    continue

            return value

    def _prompt_count(self, label: str) -> int:
        """Prompt for a non-negative whole number until a valid one is given."""
        while True:
            typed = self._read_line(f"  {label:<16}: ").replace(",", "")
            if not typed.isdigit():
                self.output_function(
                    f"    {label} must be a whole number, zero or more."
                )
                continue
            return int(typed)

    def _prompt_limit_price(self, bid: float, ask: float) -> float:
        """Prompt for the limit price.

        Typed rather than computed because min_tick comes back None from the
        API, so the valid increment cannot be derived. The app shows it.
        """
        self.output_function("")
        self.output_function(
            "  Limit price: type the price you want, at a valid increment"
        )
        self.output_function(
            "  as shown in the app. Tiger does not report the tick size."
        )

        while True:
            typed = self._read_line("  Limit price     : ")
            try:
                value = float(typed)
            except ValueError:
                self.output_function(f"    Not a number: {typed!r}")
                continue

            problem = check_price_is_positive(value, "Limit price")
            if problem:
                self.output_function(f"    {problem}")
                continue

            if is_limit_outside_spread(value, bid, ask):
                self.output_function(
                    f"    {value:,.2f} is outside the quoted market "
                    f"{bid:,.2f} / {ask:,.2f}."
                )
                if not self._confirm("    Use it anyway?"):
                    continue

            return value

    def _confirm_wide_spread(self, bid: float, ask: float) -> None:
        """Ask about a spread wide enough to look wrong."""
        if not is_spread_suspiciously_wide(bid, ask):
            return

        _spread, spread_percent = calculate_spread(bid, ask)
        self.output_function("")
        self.output_function(
            f"    The spread is {spread_percent:.1f}% of the ask "
            f"({bid:,.2f} / {ask:,.2f})."
        )
        self.output_function(
            "    That is very wide. You pay it the moment you enter."
        )
        if not self._confirm("    Are those numbers right?"):
            raise QuoteEntryError("Spread not confirmed. Nothing was simulated.")

    def _check_for_decimal_slip(self, typed_ask: float, last_trade) -> None:
        """Stop hard if the typed ask is far from the last traded price."""
        if last_trade is None:
            return

        ratio = decimal_slip_ratio(typed_ask, last_trade.close)
        if not is_decimal_slip(ratio):
            return

        phrase = build_override_phrase(typed_ask)

        self.output_function("")
        self.output_function("=" * 60)
        self.output_function(
            f"  TYPED PRICE IS {ratio:.1f}x THE LAST TRADED PRICE"
        )
        self.output_function("=" * 60)
        self.output_function(f"  You typed         : {typed_ask:,.2f}")
        self.output_function(
            f"  Last traded close : {last_trade.close:,.2f}   on "
            f"{last_trade.trade_date}  ({last_trade.days_old} days ago)"
        )
        self.output_function(f"  Ratio             : {ratio:.1f}x")
        self.output_function("")
        self.output_function("  A misplaced decimal point is the likely explanation.")
        self.output_function(
            "  At 100 shares per contract this is the difference between"
        )
        self.output_function(
            f"  ${last_trade.close * 100:,.2f} and ${typed_ask * 100:,.2f}."
        )
        self.output_function("")
        self.output_function(
            "  The last close can be days old on a thin contract, so this is"
        )
        self.output_function("  a sanity check, not a price. If it is genuinely right:")
        self.output_function("")
        self.output_function(f"  Retype it as:  {phrase}")
        self.output_function("=" * 60)

        typed = self._read_line("  > ")
        if typed.upper() != phrase.upper():
            raise QuoteEntryError(
                f"Override phrase did not match. Expected {phrase!r}, got {typed!r}.\n"
                "Nothing was simulated. Check the price and start again."
            )

        self.output_function("  Override accepted.")

    def _confirm(self, question: str) -> bool:
        """Ask a yes/no question. Anything but y is no."""
        answer = self._read_line(f"{question} [y/N] ")
        return answer.lower() in ("y", "yes")


def build_market_data_provider(settings, quote_client=None) -> MarketDataProvider:
    """Build the provider named by MARKET_DATA_SOURCE.

    The only place that chooses between implementations. Swapping to fetched
    data is one line in .env, and no other file changes.

    Args:
        settings: Validated configuration.
        quote_client: A tigeropen QuoteClient, for the decimal-slip check.

    Returns:
        The provider to use.

    Raises:
        NotImplementedError: If a real provider is requested before it exists.
    """
    if settings.market_data_source == "manual":
        return ManualEntryProvider(quote_client=quote_client)

    raise NotImplementedError(
        "MARKET_DATA_SOURCE=tiger needs a TigerQuoteProvider, which is not "
        "written yet because the usOptionQuote entitlement has not been bought. "
        "Set MARKET_DATA_SOURCE=manual in .env."
    )
