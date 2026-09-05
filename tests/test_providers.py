"""Unit tests for manual market data entry. Offline, no network.

The prompting flow is driven through injected input/output functions, so the
whole of ManualEntryProvider is exercised without a terminal.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.contract import OptionContractInfo  # noqa: E402
from api.service.market import LastTrade  # noqa: E402
# The seam itself, so this one test names the concrete class on purpose.
from api.service.market.quotes import ManualEntryProvider  # noqa: E402
from api.service.market import (  # noqa: E402
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    QuoteEntryError,
    QuoteSnapshot,
    QuoteSource,
    build_override_phrase,
    check_bid_below_ask,
    check_price_is_positive,
    decimal_slip_ratio,
    is_decimal_slip,
    is_limit_outside_spread,
    is_spread_suspiciously_wide,
)


def make_contract():
    """Build an OptionContractInfo for tests."""
    return OptionContractInfo(
        identifier="AAPL  260918C00320000",
        underlying="AAPL",
        expiry_date_text="2026-09-18",
        expiry_compact="20260918",
        strike=320.0,
        put_call="CALL",
        multiplier=100.0,
        contract_id=353122977,
        days_to_expiry=15,
        name="Apple",
    )


class ScriptedInput:
    """Replays a fixed list of answers, then raises EOF like a closed stdin."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class CapturedOutput:
    """Collects printed lines so they can be asserted on."""

    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class StubQuoteClientWithBars:
    """Stands in for the quote client used by the decimal-slip check."""

    def __init__(self, last_trade):
        self.last_trade = last_trade


def install_last_trade(monkeypatch, last_trade):
    """Make fetch_last_traded_close return a fixed value."""
    # quotes.py imports this from prices.py inside the function, so the patch
    # has to land on prices.py itself.
    from api.service.market import prices

    monkeypatch.setattr(
        prices, "fetch_last_traded_close", lambda quote_client, identifier: last_trade
    )


class TestQuoteSnapshotLabelling:
    def test_source_and_captured_at_are_required(self):
        """Labelling is structural: an unlabelled quote cannot be built."""
        with pytest.raises(TypeError):
            QuoteSnapshot(  # type: ignore[call-arg]
                bid=1.0, ask=1.1, volume=1, open_interest=1, limit_price=1.1
            )

    def test_manual_quotes_carry_the_manual_tag(self):
        quote = QuoteSnapshot(
            bid=1.0, ask=1.1, volume=1, open_interest=1, limit_price=1.1,
            source=QuoteSource.MANUAL, captured_at=datetime.now(timezone.utc),
        )
        assert quote.is_manual is True
        assert quote.source_tag == "[MANUAL]"

    def test_fetched_quotes_are_tagged_differently(self):
        quote = QuoteSnapshot(
            bid=1.0, ask=1.1, volume=1, open_interest=1, limit_price=1.1,
            source=QuoteSource.TIGER_API, captured_at=datetime.now(timezone.utc),
        )
        assert quote.is_manual is False
        assert quote.source_tag == "[TIGER]"


class TestStaleness:
    def make_quote(self, captured_at):
        return QuoteSnapshot(
            bid=1.0, ask=1.1, volume=1, open_interest=1, limit_price=1.1,
            source=QuoteSource.MANUAL, captured_at=captured_at,
        )

    def test_a_fresh_quote_is_not_stale(self):
        quote = self.make_quote(datetime.now(timezone.utc))
        assert quote.is_stale(DEFAULT_MAX_QUOTE_AGE_SECONDS) is False

    def test_a_quote_older_than_the_limit_is_stale(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert self.make_quote(old).is_stale(60) is True

    def test_the_default_limit_is_sixty_seconds(self):
        assert DEFAULT_MAX_QUOTE_AGE_SECONDS == 60

    def test_the_limit_is_configurable(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert self.make_quote(old).is_stale(120) is False


class TestOrdinaryChecks:
    def test_price_must_be_positive(self):
        assert check_price_is_positive(0.0, "Bid") is not None
        assert check_price_is_positive(-1.0, "Bid") is not None
        assert check_price_is_positive(0.01, "Bid") is None

    def test_bid_above_ask_is_rejected(self):
        assert check_bid_below_ask(5.20, 5.00) is not None
        assert check_bid_below_ask(5.00, 5.20) is None

    def test_wide_spread_is_flagged(self):
        assert is_spread_suspiciously_wide(bid=1.0, ask=10.0) is True
        assert is_spread_suspiciously_wide(bid=5.00, ask=5.20) is False

    def test_limit_outside_the_market_is_flagged(self):
        assert is_limit_outside_spread(6.00, bid=5.00, ask=5.20) is True
        assert is_limit_outside_spread(4.00, bid=5.00, ask=5.20) is True
        assert is_limit_outside_spread(5.10, bid=5.00, ask=5.20) is False


class TestDecimalSlipCheck:
    def test_ratio_is_typed_over_last_close(self):
        assert decimal_slip_ratio(52.0, 4.90) == pytest.approx(10.61, abs=0.01)

    def test_no_last_close_gives_no_ratio(self):
        assert decimal_slip_ratio(52.0, None) is None

    def test_ten_times_is_a_slip(self):
        assert is_decimal_slip(decimal_slip_ratio(52.0, 5.20)) is True

    def test_one_tenth_is_a_slip(self):
        assert is_decimal_slip(decimal_slip_ratio(0.52, 5.20)) is True

    def test_a_normal_move_is_not_a_slip(self):
        assert is_decimal_slip(decimal_slip_ratio(6.20, 5.20)) is False

    def test_missing_ratio_is_not_a_slip(self):
        """No history means the check could not run, not that it failed."""
        assert is_decimal_slip(None) is False

    def test_override_phrase_contains_the_value(self):
        assert build_override_phrase(52.0) == "USE 52.00"


class TestManualEntryFlow:
    def run_entry(self, monkeypatch, answers, last_trade=None):
        install_last_trade(monkeypatch, last_trade)
        scripted = ScriptedInput(answers)
        captured = CapturedOutput()
        provider = ManualEntryProvider(
            quote_client=StubQuoteClientWithBars(last_trade),
            input_function=scripted,
            output_function=captured,
        )
        return provider, scripted, captured

    def test_a_clean_entry_produces_a_manual_snapshot(self, monkeypatch):
        provider, _scripted, _captured = self.run_entry(
            monkeypatch,
            answers=["5.00", "5.20", "8900", "41000", "5.20"],
            last_trade=LastTrade(close=5.10, trade_date=date(2026, 9, 3), days_old=0),
        )
        quote = provider.get_quote(make_contract())

        assert quote.bid == 5.00
        assert quote.ask == 5.20
        assert quote.volume == 8900
        assert quote.open_interest == 41000
        assert quote.limit_price == 5.20
        assert quote.source is QuoteSource.MANUAL

    def test_the_decimal_check_evidence_is_kept_for_the_audit_trail(self, monkeypatch):
        provider, _scripted, _captured = self.run_entry(
            monkeypatch,
            answers=["5.00", "5.20", "8900", "41000", "5.20"],
            last_trade=LastTrade(close=5.10, trade_date=date(2026, 9, 3), days_old=0),
        )
        quote = provider.get_quote(make_contract())

        assert quote.last_close == 5.10
        assert quote.last_close_date == date(2026, 9, 3)
        assert quote.last_close_ratio == pytest.approx(5.20 / 5.10)

    def test_a_reflexive_yes_does_not_clear_a_decimal_slip(self, monkeypatch):
        """The whole point of the override phrase."""
        provider, _scripted, _captured = self.run_entry(
            monkeypatch,
            answers=["11.40", "115.00", "y"],
            last_trade=LastTrade(close=11.50, trade_date=date(2026, 9, 3), days_old=0),
        )
        with pytest.raises(QuoteEntryError, match="Override phrase did not match"):
            provider.get_quote(make_contract())

    def test_the_exact_phrase_clears_a_decimal_slip(self, monkeypatch):
        provider, _scripted, _captured = self.run_entry(
            monkeypatch,
            answers=["11.40", "115.00", "USE 115.00", "y", "8900", "41000", "115.00"],
            last_trade=LastTrade(close=11.50, trade_date=date(2026, 9, 3), days_old=0),
        )
        quote = provider.get_quote(make_contract())
        assert quote.ask == 115.00

    def test_the_skipped_check_announces_itself(self, monkeypatch):
        """Silence would imply the check passed."""
        provider, _scripted, captured = self.run_entry(
            monkeypatch,
            answers=["5.00", "5.20", "8900", "41000", "5.20"],
            last_trade=None,
        )
        provider.get_quote(make_contract())
        assert "SKIPPED" in captured.text

    def test_a_bad_price_is_re_prompted_not_fatal(self, monkeypatch):
        provider, _scripted, captured = self.run_entry(
            monkeypatch,
            answers=["nonsense", "-1", "5.00", "5.20", "8900", "41000", "5.20"],
            last_trade=None,
        )
        quote = provider.get_quote(make_contract())
        assert quote.bid == 5.00
        assert "Not a number" in captured.text

    def test_bid_above_ask_is_re_prompted(self, monkeypatch):
        provider, _scripted, captured = self.run_entry(
            monkeypatch,
            answers=["5.20", "5.00", "5.30", "8900", "41000", "5.25"],
            last_trade=None,
        )
        quote = provider.get_quote(make_contract())
        assert quote.ask == 5.30
        assert "not a real market" in captured.text

    def test_abandoning_entry_raises_rather_than_returning_junk(self, monkeypatch):
        provider, _scripted, _captured = self.run_entry(
            monkeypatch, answers=[], last_trade=None
        )
        with pytest.raises(QuoteEntryError):
            provider.get_quote(make_contract())
