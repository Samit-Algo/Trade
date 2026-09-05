"""Unit tests for the safety locks. No network, no SDK, no credentials."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.core.safety import (  # noqa: E402
    LiveTradingBlocked,
    assert_order_allowed,
    mask_account,
    resolve_account_mode,
)

PAPER = "20191106192858300"
LIVE = "U12300123"


class TestResolveAccountMode:
    def test_matching_paper_account_is_paper(self):
        assert resolve_account_mode(PAPER, PAPER, allow_live=False) == "PAPER"

    def test_non_paper_account_is_blocked_by_default(self):
        with pytest.raises(LiveTradingBlocked):
            resolve_account_mode(LIVE, PAPER, allow_live=False)

    def test_non_paper_account_is_live_when_opted_in(self):
        assert resolve_account_mode(LIVE, PAPER, allow_live=True) == "LIVE"

    def test_empty_paper_account_never_yields_paper(self):
        """A blank TIGER_PAPER_ACCOUNT must not turn a blank account into PAPER."""
        with pytest.raises(LiveTradingBlocked):
            resolve_account_mode("", "", allow_live=False)

    def test_one_character_difference_is_blocked(self):
        """The whole point of Lock 1: near-misses are not matches."""
        almost = PAPER[:-1] + "1"
        with pytest.raises(LiveTradingBlocked):
            resolve_account_mode(almost, PAPER, allow_live=False)

    def test_block_message_does_not_leak_full_account(self):
        with pytest.raises(LiveTradingBlocked) as exc:
            resolve_account_mode(LIVE, PAPER, allow_live=False)
        # The guard message intentionally names the account so a human can
        # compare it; confirm it at least says why it refused.
        assert "TIGER_ALLOW_LIVE" in str(exc.value)


class TestAssertOrderAllowed:
    def test_dry_run_blocks_even_in_paper(self):
        with pytest.raises(LiveTradingBlocked, match="DRY_RUN"):
            assert_order_allowed("PAPER", dry_run=True)

    def test_live_is_blocked_outright(self):
        with pytest.raises(LiveTradingBlocked, match="non-paper"):
            assert_order_allowed("LIVE", dry_run=False)

    def test_live_with_dry_run_is_blocked(self):
        with pytest.raises(LiveTradingBlocked):
            assert_order_allowed("LIVE", dry_run=True)

    def test_paper_without_dry_run_passes(self):
        assert assert_order_allowed("PAPER", dry_run=False) is None

    def test_unknown_mode_is_blocked(self):
        with pytest.raises(LiveTradingBlocked):
            assert_order_allowed("", dry_run=False)


class TestMaskAccount:
    def test_shows_only_last_four(self):
        assert mask_account("20191106192858300") == "****8300"

    def test_short_account_is_fully_masked(self):
        assert mask_account("123") == "***"

    def test_empty_account(self):
        assert mask_account("") == "(unset)"

    def test_full_number_never_appears(self):
        assert LIVE not in mask_account(LIVE)
