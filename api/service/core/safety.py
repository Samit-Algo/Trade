"""The locks, and the record of what got past them.

    Safety    the four locks that decide whether an order may be sent at all
    Audit     what was actually sent, written down before and after

Separate concerns that answer the same question from either side: may this
happen, and what happened.
"""

from __future__ import annotations

import sys
import json
from datetime import datetime, timezone
from pathlib import Path


# --------------------------------------------------------------------------
# SAFETY
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# AUDIT
# --------------------------------------------------------------------------

#: Appended to, never rewritten. logs/ is gitignored.
#: core/ -> service/ -> api/ -> the repo root, then logs/ (gitignored).
LOG_DIRECTORY = Path(__file__).resolve().parents[3] / "logs"
ORDER_LOG_PATH = LOG_DIRECTORY / "order_audit.log"


def build_order_record(
    settings,
    contract,
    quote,
    estimate,
    stage: str,
    order_id: str | None = None,
    outcome: str | None = None,
    legs=None,
) -> dict:
    """Assemble one audit record.

    Args:
        settings: Validated configuration, for the masked account and mode.
        contract: The OptionContractInfo the order refers to.
        quote: The QuoteSnapshot the estimate was built from.
        estimate: The CostEstimate.
        stage: What happened -- "SIMULATED" here. Phase 5 will add its own.
        order_id: The broker's order ID, once there is one.
        outcome: The final fill outcome, once it is known.
        legs: A BracketLegs, when the order carried attached orders.

    Returns:
        A JSON-serialisable dict.
    """
    now = datetime.now(timezone.utc)

    return {
        "timestamp": now.isoformat(),
        "stage": stage,
        # SIMULATED records must never be mistakable for submitted ones.
        "submitted": False if stage == "SIMULATED" else None,
        "account_masked": mask_account(settings.account),
        "mode": settings.mode,
        "dry_run": settings.dry_run,
        "contract": {
            "identifier": contract.identifier,
            "underlying": contract.underlying,
            "expiry": contract.expiry_date_text,
            "strike": contract.strike,
            "put_call": contract.put_call,
            "multiplier": contract.multiplier,
            "days_to_expiry": contract.days_to_expiry,
        },
        "order": {
            "action": estimate.action,
            "quantity": estimate.quantity,
            "order_type": "LMT",
            "limit_price": quote.limit_price,
            "limit_price_typed": quote.is_manual,
            "price_used": estimate.price_used,
            "price_reason": estimate.price_reason,
            "computed_cash": estimate.total_cash,
            "break_even": estimate.break_even_price,
            "maximum_loss": estimate.maximum_loss,
        },
        # The four market values, exactly as typed. This is the block that
        # makes "mistype or market move?" answerable after the fact.
        "quote": {
            "source": quote.source.value,
            "typed_bid": quote.bid,
            "typed_ask": quote.ask,
            "typed_volume": quote.volume,
            "captured_at": quote.captured_at.isoformat(),
            "age_seconds_at_send": round(quote.age_seconds(now), 1),
        },
        # What the decimal-slip check saw, including when it could not run.
        "decimal_check": {
            "last_close": quote.last_close,
            "last_close_date": (
                quote.last_close_date.isoformat() if quote.last_close_date else None
            ),
            "ratio": (
                round(quote.last_close_ratio, 4)
                if quote.last_close_ratio is not None
                else None
            ),
            "performed": quote.last_close is not None,
        },
        # What was actually sent on the wire for the attached orders. A
        # bracketed order cannot be previewed by the broker, so if one is
        # rejected this block is the only record of what it was asked to do.
        "legs": (
            {
                "attach_type": legs.attach_type,
                "leg_time_in_force": legs.leg_time_in_force,
                "take_profit": {
                    "leg_type": "PROFIT",
                    "price": legs.take_profit_price,
                    "time_in_force": legs.leg_time_in_force,
                },
                "stop_loss": {
                    "leg_type": "LOSS",
                    "price": legs.stop_loss_price,
                    "time_in_force": legs.leg_time_in_force,
                },
            }
            if legs is not None
            else None
        ),
        "order_id": order_id,
        "outcome": outcome,
    }


def write_order_record(record: dict, log_path: Path | None = None) -> Path:
    """Append one record to the audit log.

    Args:
        record: The record from build_order_record.
        log_path: Where to write, or None for the default.

    Returns:
        The path written to.
    """
    path = log_path if log_path is not None else ORDER_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    return path
