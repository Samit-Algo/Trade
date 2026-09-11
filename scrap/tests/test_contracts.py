"""Unit tests for Phase 3 contract resolution.

Offline. The two API-shaped functions are exercised with stub clients that
reproduce the SDK's actual quirks -- string strikes, an empty list for an
unlisted expiry -- because those are the behaviours most likely to regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.service import contract as contracts  # noqa: E402
from api.service.contract import (  # noqa: E402
    ContractError,
    ExpiredContractError,
    ExpiryNotListedError,
    build_identifier,
    find_nearest_strikes,
    from_tiger_expiry_format,
    identifier_from_ladder_entry,
    list_strikes_for_expiry,
    parse_identifier,
    read_min_tick,
    resolve_expiry,
    to_tiger_expiry_format,
    validate_option_type,
)
from api.service.market import OptionExpiry, parse_expiry_date  # noqa: E402


class StubLadderContract:
    """An entry as get_derivative_contracts really returns it.

    strike is a STRING, identifier is None, and the OCC code lives in name.
    """

    def __init__(self, strike_text: str, put_call: str):
        self.strike = strike_text
        self.put_call = put_call
        self.identifier = None
        self.name = f"AAPL  260918{'C' if put_call == 'CALL' else 'P'}00320000"
        self.min_tick = None


class StubTradeClient:
    """A TradeClient stub returning a fixed ladder."""

    def __init__(self, contracts_to_return):
        self.contracts_to_return = contracts_to_return

    def get_derivative_contracts(self, symbol, sec_type, expiry, lang=None):
        return self.contracts_to_return


def make_expiry(date_text: str, days_to_expiry: int) -> OptionExpiry:
    """Build an OptionExpiry for tests."""
    return OptionExpiry(
        date_text=date_text,
        expiry_date=parse_expiry_date(date_text),
        timestamp_ms=0,
        period_tag="w",
        days_to_expiry=days_to_expiry,
        option_symbol="AAPL",
    )


class TestExpiryFormatConversion:
    def test_to_compact(self):
        assert to_tiger_expiry_format("2026-09-18") == "20260918"

    def test_from_compact(self):
        assert from_tiger_expiry_format("20260918") == "2026-09-18"

    def test_round_trip(self):
        assert from_tiger_expiry_format(to_tiger_expiry_format("2026-01-02")) == "2026-01-02"

    def test_rejects_wrong_format(self):
        with pytest.raises(ContractError, match="YYYY-MM-DD"):
            to_tiger_expiry_format("18/09/2026")

    def test_rejects_compact_passed_as_dashed(self):
        """The exact mix-up this helper exists to prevent."""
        with pytest.raises(ContractError):
            to_tiger_expiry_format("20260918")


class TestValidateOptionType:
    def test_accepts_call_and_put(self):
        assert validate_option_type("CALL") == "CALL"
        assert validate_option_type("PUT") == "PUT"

    def test_normalises_case_and_whitespace(self):
        assert validate_option_type("  call ") == "CALL"

    def test_rejects_anything_else(self):
        with pytest.raises(ContractError, match="CALL or PUT"):
            validate_option_type("CALLS")


class TestIdentifierHelpers:
    def test_build_matches_the_documented_shape(self):
        identifier = build_identifier("AAPL", "20260918", "CALL", 320)
        assert identifier.strip() == "AAPL  260918C00320000".strip()

    def test_round_trip_through_both_directions(self):
        identifier = build_identifier("AAPL", "20260918", "PUT", 300)
        underlying, expiry, put_call, strike = parse_identifier(identifier)
        assert underlying == "AAPL"
        assert put_call == "PUT"
        assert strike == 300.0

    def test_parse_rejects_rubbish(self):
        with pytest.raises(ContractError):
            parse_identifier("not an identifier")


class TestFindNearestStrikes:
    def test_picks_the_closest_by_distance(self):
        """Closest by distance, not a symmetric window.

        317.13 sits between 315 and 320, so 312.5 (4.63 away) beats
        322.5 (5.37 away) even though it is on the same side as 315.
        """
        available = [310.0, 312.5, 315.0, 317.5, 320.0, 322.5, 325.0]
        nearest = find_nearest_strikes(available, 317.13, count=2)
        assert nearest == [312.5, 315.0, 317.5, 320.0]

    def test_returns_ascending(self):
        available = [100.0, 200.0, 300.0]
        assert find_nearest_strikes(available, 250.0) == sorted(
            find_nearest_strikes(available, 250.0)
        )

    def test_empty_input_gives_empty_output(self):
        assert find_nearest_strikes([], 100.0) == []

    def test_wanted_far_below_every_strike(self):
        available = [100.0, 105.0, 110.0]
        assert find_nearest_strikes(available, 1.0, count=1) == [100.0, 105.0]


class TestListStrikesForExpiry:
    """The two ladder quirks, reproduced exactly."""

    def test_string_strikes_are_converted_and_sorted_numerically(self):
        """'100.0' must not sort before '95.0'."""
        ladder = [
            StubLadderContract("100.0", "CALL"),
            StubLadderContract("95.0", "CALL"),
            StubLadderContract("320.0", "CALL"),
        ]
        strikes = list_strikes_for_expiry(
            StubTradeClient(ladder), "AAPL", "20260918", "CALL"
        )
        assert strikes == [95.0, 100.0, 320.0]
        assert all(isinstance(value, float) for value in strikes)

    def test_only_the_requested_side_is_returned(self):
        ladder = [
            StubLadderContract("320.0", "CALL"),
            StubLadderContract("330.0", "PUT"),
        ]
        assert list_strikes_for_expiry(
            StubTradeClient(ladder), "AAPL", "20260918", "PUT"
        ) == [330.0]

    def test_empty_list_means_unlisted_expiry_not_no_strikes(self):
        """An unlisted expiry returns [], which must not read as 'no strikes'."""
        with pytest.raises(ExpiryNotListedError, match="does not exist"):
            list_strikes_for_expiry(
                StubTradeClient([]), "AAPL", "20260917", "CALL"
            )


class TestLadderFieldQuirks:
    def test_identifier_is_read_from_name_on_ladder_entries(self):
        entry = StubLadderContract("320.0", "CALL")
        assert entry.identifier is None
        assert identifier_from_ladder_entry(entry) == "AAPL  260918C00320000"

    def test_min_tick_is_none(self):
        assert read_min_tick(StubLadderContract("320.0", "CALL")) is None


class TestResolveExpiry:
    """The three-outcome rule. This is the requirement from scrap/HANDOVER.md section 6."""

    def stub_expirations(self, monkeypatch, expiries):
        # Patch the name where the lookup HAPPENS: the contract module holds
        # its own reference to the imported function, so patching the market
        # module it came from would not be seen here.
        from api.service import contract

        monkeypatch.setattr(
            contract, "list_expirations", lambda quote_client, underlying: expiries
        )

    def test_listed_and_current_resolves(self, monkeypatch):
        expiries = [make_expiry("2026-09-18", 15)]
        self.stub_expirations(monkeypatch, expiries)
        resolved = resolve_expiry(None, "AAPL", "2026-09-18")
        assert resolved.date_text == "2026-09-18"

    def test_not_listed_says_not_listed(self, monkeypatch):
        expiries = [make_expiry("2026-09-18", 15)]
        self.stub_expirations(monkeypatch, expiries)
        with pytest.raises(ExpiryNotListedError, match="not a listed expiration"):
            resolve_expiry(None, "AAPL", "2026-09-19")

    def test_listed_but_past_says_expired_not_not_found(self, monkeypatch):
        """The whole point: a real date that has passed is EXPIRED, not missing."""
        expiries = [
            make_expiry("2026-09-02", -1),
            make_expiry("2026-09-04", 1),
        ]
        self.stub_expirations(monkeypatch, expiries)

        with pytest.raises(ExpiredContractError) as raised:
            resolve_expiry(None, "AAPL", "2026-09-02")

        message = str(raised.value)
        assert "EXPIRED" in message
        assert "not a listed expiration" not in message

    def test_expired_error_names_the_next_tradable_expiry(self, monkeypatch):
        expiries = [
            make_expiry("2026-09-02", -1),
            make_expiry("2026-09-04", 1),
        ]
        self.stub_expirations(monkeypatch, expiries)

        with pytest.raises(ExpiredContractError, match="2026-09-04"):
            resolve_expiry(None, "AAPL", "2026-09-02")

    def test_expired_and_not_listed_are_different_types(self, monkeypatch):
        """A caller must be able to tell them apart without reading the text."""
        assert not issubclass(ExpiredContractError, ExpiryNotListedError)
        assert not issubclass(ExpiryNotListedError, ExpiredContractError)

    def test_not_listed_message_offers_only_tradable_dates(self, monkeypatch):
        """Suggesting an already-expired date as an alternative would be absurd."""
        expiries = [
            make_expiry("2026-09-02", -1),
            make_expiry("2026-09-04", 1),
        ]
        self.stub_expirations(monkeypatch, expiries)

        with pytest.raises(ExpiryNotListedError) as raised:
            resolve_expiry(None, "AAPL", "2027-01-01")

        message = str(raised.value)
        assert "2026-09-04" in message
        assert "2026-09-02" not in message
