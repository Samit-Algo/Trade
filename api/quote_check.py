"""Running the Phase 4 sanity checks against a quote that arrived in a body.

The CLI asks a human for five numbers and re-prompts on a bad one. Over HTTP
there is nobody to re-prompt, so the same checks run against the supplied
values and a failure becomes a 400 that names which check failed and what was
wrong. Nothing is weakened; only the recovery differs.

The decimal-slip check keeps its special status. At the CLI it demands the
value retyped inside a phrase that a reflexive "y" cannot clear. Over HTTP the
equivalent is an explicit boolean the client must set on a second, deliberate
request -- never a query parameter, never defaulted true.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tiger_backend.market import LastTrade, calculate_spread, fetch_last_traded_close
from tiger_backend.providers import (
    QuoteSnapshot,
    QuoteSource,
    check_bid_below_ask,
    check_price_is_positive,
    decimal_slip_ratio,
    is_decimal_slip,
    is_limit_outside_spread,
    is_spread_suspiciously_wide,
)

from .errors import ApiError


def look_up_last_trade(quote_client, identifier: str) -> LastTrade | None:
    """Fetch the contract's last traded close for the decimal-slip check.

    Free: historical option bars need no quote entitlement.

    Args:
        quote_client: A tigeropen QuoteClient.
        identifier: The contract identifier.

    Returns:
        The last trade, or None when the contract has never traded.
    """
    return fetch_last_traded_close(quote_client, identifier)


def check_ordinary_rules(quote_input) -> None:
    """Run the checks that would re-prompt at the CLI.

    Args:
        quote_input: The QuoteInput from the request body.

    Raises:
        ApiError: 400 naming the failed check.
    """
    problems = []

    for value, label in ((quote_input.bid, "bid"), (quote_input.ask, "ask"),
                         (quote_input.limit_price, "limit_price")):
        problem = check_price_is_positive(value, label)
        if problem:
            problems.append({"field": label, "problem": problem})

    ordering_problem = check_bid_below_ask(quote_input.bid, quote_input.ask)
    if ordering_problem:
        problems.append({"field": "bid/ask", "problem": ordering_problem})

    if problems:
        raise ApiError(
            status_code=400,
            error_code="QUOTE_REJECTED",
            message="The quote did not pass the sanity checks. Fix the fields "
                    "listed and submit again.",
            detail={"failed_checks": problems},
        )


def collect_quote_warnings(quote_input) -> list[str]:
    """Gather the things a human would have been asked to confirm.

    These do not block over HTTP. At the CLI they are yes/no prompts, and an
    API has no one to ask, so they are surfaced as warnings on the preview for
    a human to read before submitting.

    Args:
        quote_input: The QuoteInput from the request body.

    Returns:
        Warning lines, possibly empty.
    """
    warnings = []

    if is_spread_suspiciously_wide(quote_input.bid, quote_input.ask):
        _spread, spread_percent = calculate_spread(quote_input.bid, quote_input.ask)
        warnings.append(
            f"The spread is {spread_percent:.1f}% of the ask "
            f"({quote_input.bid:,.2f} / {quote_input.ask:,.2f}). That is very "
            "wide, and you pay it the moment you enter."
        )

    if is_limit_outside_spread(
        quote_input.limit_price, quote_input.bid, quote_input.ask
    ):
        warnings.append(
            f"The limit price {quote_input.limit_price:,.2f} is outside the "
            f"quoted market {quote_input.bid:,.2f} / {quote_input.ask:,.2f}. "
            "Legal, and occasionally deliberate, but usually a mistake."
        )

    return warnings


def check_for_decimal_slip(
    typed_ask: float,
    last_trade: LastTrade | None,
    override_confirmed: bool,
    multiplier: float,
) -> None:
    """Block a price that looks like a misplaced decimal point.

    The most expensive typing error available: it turns $520 into $5,200. It is
    its own failure mode rather than one warning among several, and clearing it
    takes a deliberate second request with confirm_price_override set.

    Args:
        typed_ask: The ask as supplied.
        last_trade: The contract's last traded close, or None.
        override_confirmed: Whether the client explicitly overrode.
        multiplier: Shares per contract, for the cash comparison.

    Raises:
        ApiError: 400 carrying the full evidence, when the ratio is off and no
            override was given.
    """
    if last_trade is None:
        # Nothing to compare against. The preview says so rather than staying
        # silent, because silence would imply the check passed.
        return

    ratio = decimal_slip_ratio(typed_ask, last_trade.close)
    if not is_decimal_slip(ratio):
        return

    if override_confirmed:
        return

    raise ApiError(
        status_code=400,
        error_code="PRICE_LOOKS_LIKE_DECIMAL_SLIP",
        message=(
            f"The ask you sent, {typed_ask:,.2f}, is {ratio:.1f}x the price this "
            f"contract last actually traded at ({last_trade.close:,.2f}). A "
            "misplaced decimal point is the likely explanation. At "
            f"{multiplier:,.0f} shares per contract this is the difference "
            f"between ${last_trade.close * multiplier:,.2f} and "
            f"${typed_ask * multiplier:,.2f}. If the price is genuinely right, "
            "send the request again with confirm_price_override set to true."
        ),
        detail={
            "typed_value": typed_ask,
            "last_close": last_trade.close,
            "last_close_date": last_trade.trade_date.isoformat(),
            "last_close_age_days": last_trade.days_old,
            "ratio": round(ratio, 4),
            "resubmit_with": {"confirm_price_override": True},
            "note": (
                "The last close can be days old on a thin contract, so this is "
                "a sanity check and not a price."
            ),
        },
    )


def build_quote_snapshot(quote_input, last_trade: LastTrade | None) -> QuoteSnapshot:
    """Turn a validated request body into the same snapshot the CLI produces.

    Stamped MANUAL because a human read these numbers off a screen and typed
    them into a client. Nothing about arriving over HTTP makes them fetched.

    Args:
        quote_input: The QuoteInput from the request body.
        last_trade: What the decimal-slip check compared against, for the audit.

    Returns:
        A QuoteSnapshot ready for the library.
    """
    ratio = decimal_slip_ratio(
        quote_input.ask, last_trade.close if last_trade else None
    )

    return QuoteSnapshot(
        bid=quote_input.bid,
        ask=quote_input.ask,
        volume=quote_input.volume,
        open_interest=quote_input.open_interest,
        limit_price=quote_input.limit_price,
        source=QuoteSource.MANUAL,
        captured_at=datetime.now(timezone.utc),
        last_close=last_trade.close if last_trade else None,
        last_close_date=last_trade.trade_date if last_trade else None,
        last_close_ratio=ratio,
    )
