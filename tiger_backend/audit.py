"""The audit trail.

One JSON object per line in logs/order_audit.log, appended and never rewritten.

Why this exists in the shape it does: when a fill comes back wrong, exactly one
question matters -- did the market move, or did the human mistype? Without the
typed values recorded next to the outcome that is unanswerable afterwards. With
them it is a two-line comparison.

The account number is masked. Nothing else about the order is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .safety import mask_account

#: Appended to, never rewritten. logs/ is gitignored.
LOG_DIRECTORY = Path(__file__).resolve().parent.parent / "logs"
ORDER_LOG_PATH = LOG_DIRECTORY / "order_audit.log"


def build_order_record(
    settings,
    contract,
    quote,
    estimate,
    stage: str,
    order_id: str | None = None,
    outcome: str | None = None,
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
            "typed_open_interest": quote.open_interest,
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
