"""Account safety guards.

This module is the reason the project exists in this shape. The Tiger SDK
treats a paper account and a live account identically -- the only thing that
distinguishes them is the account ID string in the configuration. A single
wrong character means real money.

Three independent locks must all be satisfied before an order can reach Tiger:

    Lock 1 -- Account allowlist : configured account must equal TIGER_PAPER_ACCOUNT
    Lock 2 -- Live opt-in       : TIGER_ALLOW_LIVE must be true for any other account
    Lock 3 -- Dry run           : DRY_RUN must be false for an order to be sent

With the shipped defaults the system is physically incapable of placing an
order. That is intentional.

Nothing in this module performs I/O against Tiger. It is pure logic plus the
startup banner, so it can be unit-tested with no network.
"""

from __future__ import annotations

import sys

BANNER_WIDTH = 60

#: Typed exactly, by a human, before a LIVE session may continue.
LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THIS IS A LIVE ACCOUNT"


class LiveTradingBlocked(Exception):
    """Raised when an order would touch an account that is not explicitly permitted."""


def resolve_account_mode(configured_account: str, paper_account: str, allow_live: bool) -> str:
    """Return 'PAPER' or 'LIVE'. Raise LiveTradingBlocked if live is not opted into."""
    if paper_account and configured_account == paper_account:
        return "PAPER"
    if allow_live:
        return "LIVE"
    raise LiveTradingBlocked(
        f"Account {configured_account!r} is not the declared paper account "
        f"({paper_account!r}) and TIGER_ALLOW_LIVE is false. Refusing to continue."
    )


def assert_order_allowed(mode: str, dry_run: bool) -> None:
    """Called immediately before place_order. Raises if the order must not be sent."""
    if dry_run:
        raise LiveTradingBlocked("DRY_RUN is true. No order will be submitted.")
    if mode != "PAPER":
        raise LiveTradingBlocked("Refusing to submit a non-paper order in this build.")


def mask_account(account: str) -> str:
    """Return a display form of an account number exposing at most the last 4 digits.

    The full account number must never reach stdout or a log file.
    """
    if not account:
        return "(unset)"
    if len(account) <= 4:
        # Too short to mask meaningfully; show nothing rather than leak it whole.
        return "*" * len(account)
    return "****" + account[-4:]


def _print_paper_banner(masked: str, dry_run: bool) -> None:
    line = "=" * BANNER_WIDTH
    print(line)
    print("  TIGER OPTIONS BACKEND")
    print(f"  Account : {masked}        (last 4 only)")
    print("  Mode    : PAPER")
    print(f"  Dry run : {'TRUE' if dry_run else 'FALSE'}")
    print(line)


def _print_live_banner(masked: str, dry_run: bool) -> None:
    """Deliberately loud. A LIVE session must never be mistaken for a paper one."""
    red = "\033[1;37;41m"  # bold white on red
    reset = "\033[0m"
    line = "!" * BANNER_WIDTH

    def loud(text: str = "") -> None:
        print(f"{red}{text.ljust(BANNER_WIDTH)}{reset}")

    print()
    loud(line)
    loud("!!  TIGER OPTIONS BACKEND  --  L I V E   A C C O U N T  !!")
    loud(line)
    loud(f"  ACCOUNT : {masked}        (LAST 4 ONLY)")
    loud("  MODE    : LIVE   ***  REAL MONEY  ***")
    loud(f"  DRY RUN : {'TRUE' if dry_run else 'FALSE'}")
    loud(line)
    loud("  ORDERS ARE STILL BLOCKED BY assert_order_allowed() IN THIS")
    loud("  BUILD. IF YOU DID NOT INTEND A LIVE ACCOUNT, STOP NOW.")
    loud(line)
    print()


def print_startup_banner(masked_account: str, mode: str, dry_run: bool) -> None:
    """Print the mandatory startup banner. Every entry point calls this first.

    In LIVE mode the banner is visually loud and the program pauses for a typed
    confirmation before continuing. A non-interactive stdin is treated as a
    refusal, so an unattended run can never sail past this prompt.
    """
    if mode == "PAPER":
        _print_paper_banner(masked_account, dry_run)
        return

    _print_live_banner(masked_account, mode == "LIVE" and dry_run)

    if not sys.stdin.isatty():
        raise LiveTradingBlocked(
            "LIVE mode requires an interactive typed confirmation, but stdin is "
            "not a terminal. Refusing to continue."
        )

    print(f'To continue, type exactly:  {LIVE_CONFIRMATION_PHRASE}')
    try:
        typed = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        raise LiveTradingBlocked("LIVE confirmation aborted. Refusing to continue.") from None

    if typed != LIVE_CONFIRMATION_PHRASE:
        raise LiveTradingBlocked(
            "LIVE confirmation phrase did not match. Refusing to continue."
        )
    print("Confirmed. Continuing in LIVE mode (orders remain blocked in this build).")
    print()
