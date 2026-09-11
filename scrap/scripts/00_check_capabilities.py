"""Diagnostic -- report which Tiger API endpoints this account can reach.

Outside the phase sequence. Strictly read-only: it calls each endpoint once
with a harmless argument and reports what came back. It places no orders,
constructs no orders, and modifies nothing.

    python scripts/00_check_capabilities.py
    python scripts/00_check_capabilities.py --grab        # claim device access
    python scripts/00_check_capabilities.py --timeout 20

Every result is one of three outcomes:

    OK       the endpoint returned data
    DENIED   permission refused -- an entitlement that has not been bought.
             This is expected and informative, not a failure.
    ERROR    anything else, and the message is shown. This is a real problem.

Output is printed and written to capabilities-<date>-<grab|nograb>.txt so two
runs can be diffed before and after buying a market data package. The filename
records whether market data device access was claimed, because that changes
results independently of any purchase.

Every signature below was verified against https://docs-en.itigerup.com/docs/
before being called. A TypeError from a wrong signature would look exactly
like a capability problem, so nothing here is guessed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.core.broker import (  # noqa: E402
    ClientSetupError,
    build_quote_client,
    build_trade_client,
)
from api.service.core.config import ConfigError, load_settings  # noqa: E402
from api.service.core.safety import LiveTradingBlocked, print_startup_banner  # noqa: E402
from api.service.core.broker import (  # noqa: E402
    BARS_LIMITER,
    CHAIN_LIMITER,
    DELAYED_STOCK_BRIEFS_LIMITER,
    EXPIRATIONS_LIMITER,
    KLINE_QUOTA_LIMITER,
    MANAGED_ACCOUNTS_LIMITER,
    MARKET_STATUS_LIMITER,
    OPEN_ORDERS_LIMITER,
    OPTION_ANALYSIS_LIMITER,
    OPTION_BARS_LIMITER,
    OPTION_BRIEFS_LIMITER,
    OPTION_DEPTH_LIMITER,
    OPTION_TIMELINE_LIMITER,
    OPTION_TRADE_TICKS_LIMITER,
    ORDERS_LIMITER,
    POSITIONS_LIMITER,
    PRIME_ASSETS_LIMITER,
    QUOTE_PERMISSION_LIMITER,
    STOCK_BRIEFS_LIMITER,
    TRADE_TICKS_LIMITER,
)

#: A liquid, always-listed symbol, so a wrong answer means a capability
#: problem rather than a badly chosen test case.
TEST_SYMBOL = "AAPL"

OK = "OK"
DENIED = "DENIED"
ERROR = "ERROR"

DEFAULT_TIMEOUT_SECONDS = 30

RULE_WIDTH = 92

#: Permission keys Tiger uses in get_quote_permission, mapped to plain English.
#: From https://docs-en.itigerup.com/docs/quote-common
PERMISSION_DESCRIPTIONS = {
    "usQuoteBasic": "US stock L1 market data",
    "usStockQuote": "US real-time stock market data",
    "usStockQuoteLv2Arca": "US stock ARCA L2",
    "usStockQuoteLv2Totalview": "US stock L2 (Totalview)",
    "usOptionQuote": "US option L1 market data",
    "usOptionQuoteLv2": "US option L2 market data",
    "usQuoteOtc": "US OTC market data",
    "usOvernight": "US overnight market data",
    "hkStockQuoteLv2": "HK stock L2 (mainland users)",
    "hkStockQuoteLv2Global": "HK stock L2 (non-mainland users)",
    "futureQuote": "Futures market data",
    "aStockQuoteLv1": "China A-share L1",
}


#: Maps the market named in a refusal to the permission key that would fix it,
#: so the summary says what to buy rather than only what failed.
MARKET_TO_PERMISSION_KEY = {
    "US OPT": "usOptionQuote",
    "US": "usStockQuote",
    "HK": "hkStockQuoteLv2Global",
}


@dataclass
class ProbeResult:
    """What happened when one endpoint was called."""

    category: str
    endpoint: str
    outcome: str  # OK, DENIED or ERROR
    detail: str = ""
    entitlement: str = ""  # named only when the error says which one


@dataclass
class ProbeContext:
    """Values discovered during the run and reused by later probes.

    The identifier-based option endpoints need a real contract. If the chain
    is reachable we take one from it; otherwise the probe says so, because an
    error about a contract that may not exist proves nothing about capability.
    """

    expiry_date_text: str = ""
    option_identifier: str = ""
    identifier_is_real: bool = False
    notes: list[str] = field(default_factory=list)


def classify_exception(error: Exception) -> tuple[str, str, str]:
    """Decide whether an exception is a permission refusal or a real error.

    Tiger reports a missing entitlement as error code 4000, "permission
    denied", usually naming the market in parentheses. That is not a failure:
    it is the answer to the question this script asks.

    Args:
        error: The exception raised by the SDK call.

    Returns:
        A triple of (outcome, detail, entitlement name or "").
    """
    message = str(error)
    lowered = message.lower()

    is_permission_problem = "permission denied" in lowered or "4000:" in lowered
    if not is_permission_problem:
        return ERROR, f"{type(error).__name__}: {message}", ""

    # The useful part is the parenthetical, e.g.
    # "(Current user and device do not have permissions in the US OPT quote market)"
    entitlement = ""
    match = re.search(r"\(([^)]*)\)", message)
    if match:
        inside_brackets = match.group(1)
        market_match = re.search(
            r"permissions in the (.+?)\s+(?:quote\s+)?market",
            inside_brackets,
            re.IGNORECASE,
        )
        if market_match:
            entitlement = market_match.group(1).strip()
        else:
            entitlement = inside_brackets.strip()

    return DENIED, "permission denied", entitlement


def looks_empty(value: Any) -> bool:
    """Decide whether a returned value carries no rows.

    Args:
        value: Whatever the SDK returned.

    Returns:
        True when the result is None or an empty list/DataFrame.
    """
    if value is None:
        return True
    if hasattr(value, "empty"):
        return bool(value.empty)
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _pluralise(count: int, noun: str) -> str:
    """Format a count with a correctly pluralised noun.

    Args:
        count: How many.
        noun: The singular noun.

    Returns:
        e.g. "1 row" or "24 rows".
    """
    if count == 1:
        return f"1 {noun}"
    return f"{count} {noun}s"


def describe_result(value: Any) -> str:
    """Summarise a successful result in a few words.

    Args:
        value: Whatever the SDK returned.

    Returns:
        A short description, e.g. "24 rows" or "empty".
    """
    if looks_empty(value):
        return "returned nothing (endpoint reachable)"

    if hasattr(value, "shape"):
        return _pluralise(value.shape[0], "row")

    if isinstance(value, (list, tuple)):
        return _pluralise(len(value), "item")

    if isinstance(value, dict):
        return _pluralise(len(value), "key")

    return "returned data"


def run_one_probe(
    category: str,
    endpoint: str,
    limiter,
    call: Callable[[], Any],
    timeout_seconds: int,
) -> ProbeResult:
    """Call one endpoint once and classify what happened.

    The call runs on a worker thread so a hung request times out instead of
    blocking the whole run. A timed-out request cannot be cancelled -- it is
    left to finish on a daemon thread and the process exits regardless.

    Args:
        category: Group heading for the report.
        endpoint: The SDK method name.
        limiter: The RateLimiter guarding this endpoint.
        call: A no-argument callable performing the request.
        timeout_seconds: How long to wait before giving up.

    Returns:
        The classified result.
    """
    limiter.wait()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(call)
        try:
            value = future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            return ProbeResult(
                category=category,
                endpoint=endpoint,
                outcome=ERROR,
                detail=f"timed out after {timeout_seconds}s",
            )
        except Exception as error:  # noqa: BLE001 - classification is the point
            outcome, detail, entitlement = classify_exception(error)
            return ProbeResult(
                category=category,
                endpoint=endpoint,
                outcome=outcome,
                detail=detail,
                entitlement=entitlement,
            )
    finally:
        # Do not wait: a hung request must not hold the script open.
        executor.shutdown(wait=False)

    return ProbeResult(
        category=category,
        endpoint=endpoint,
        outcome=OK,
        detail=describe_result(value),
    )


def probe_account_endpoints(trade_client, timeout_seconds: int) -> list[ProbeResult]:
    """Probe the account and trading queries.

    All read-only. Nothing here submits, modifies or cancels an order.

    Args:
        trade_client: A tigeropen TradeClient.
        timeout_seconds: Per-call timeout.

    Returns:
        One result per endpoint.
    """
    from tigeropen.common.consts import Market, SecurityType

    category = "Account and trading"
    results = []

    results.append(
        run_one_probe(
            category,
            "get_managed_accounts",
            MANAGED_ACCOUNTS_LIMITER,
            lambda: trade_client.get_managed_accounts(),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_prime_assets",
            PRIME_ASSETS_LIMITER,
            lambda: trade_client.get_prime_assets(base_currency="USD"),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_positions",
            POSITIONS_LIMITER,
            lambda: trade_client.get_positions(sec_type=SecurityType.STK),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_orders",
            ORDERS_LIMITER,
            lambda: trade_client.get_orders(limit=1, market=Market.US),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_open_orders",
            OPEN_ORDERS_LIMITER,
            lambda: trade_client.get_open_orders(market=Market.US),
            timeout_seconds,
        )
    )

    return results


def probe_permission_endpoints(quote_client, timeout_seconds: int) -> list[ProbeResult]:
    """Probe the entitlement queries the SDK exposes.

    get_quote_permission reports what the account holds. Its sibling
    grab_quote_permission is deliberately NOT called: it moves market data
    device access to this machine and takes it away from whatever held it
    before, which is a modification, not a query.

    Args:
        quote_client: A tigeropen QuoteClient.
        timeout_seconds: Per-call timeout.

    Returns:
        One result per endpoint.
    """
    category = "Entitlements"
    results = []

    results.append(
        run_one_probe(
            category,
            "get_quote_permission",
            QUOTE_PERMISSION_LIMITER,
            lambda: quote_client.get_quote_permission(),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_kline_quota",
            KLINE_QUOTA_LIMITER,
            lambda: quote_client.get_kline_quota(),
            timeout_seconds,
        )
    )

    return results


def probe_option_endpoints(
    quote_client,
    context: ProbeContext,
    timeout_seconds: int,
) -> list[ProbeResult]:
    """Probe the option market data endpoints.

    Runs in dependency order so that later probes can use a real expiry and a
    real contract identifier discovered by the earlier ones.

    Args:
        quote_client: A tigeropen QuoteClient.
        context: Carries discovered values between probes.
        timeout_seconds: Per-call timeout.

    Returns:
        One result per endpoint.
    """
    from tigeropen.common.consts import Market

    category = "Options market data"
    results = []

    expirations_result = run_one_probe(
        category,
        "get_option_expirations",
        EXPIRATIONS_LIMITER,
        lambda: quote_client.get_option_expirations(
            symbols=[TEST_SYMBOL], market=Market.US
        ),
        timeout_seconds,
    )
    results.append(expirations_result)

    if expirations_result.outcome == OK:
        _capture_expiry(quote_client, context)

    expiry_for_chain = context.expiry_date_text or "2026-12-18"

    chain_result = run_one_probe(
        category,
        "get_option_chain",
        CHAIN_LIMITER,
        lambda: quote_client.get_option_chain(
            symbol=TEST_SYMBOL, expiry=expiry_for_chain, market=Market.US
        ),
        timeout_seconds,
    )
    results.append(chain_result)

    _capture_identifier(quote_client, context, chain_result, expiry_for_chain)

    identifier = context.option_identifier

    results.append(
        run_one_probe(
            category,
            "get_option_briefs",
            OPTION_BRIEFS_LIMITER,
            lambda: quote_client.get_option_briefs(
                identifiers=[identifier], market=Market.US
            ),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_option_depth",
            OPTION_DEPTH_LIMITER,
            lambda: quote_client.get_option_depth(
                identifiers=[identifier], market=Market.US
            ),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_option_trade_ticks",
            OPTION_TRADE_TICKS_LIMITER,
            lambda: quote_client.get_option_trade_ticks(identifiers=[identifier]),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_option_bars",
            OPTION_BARS_LIMITER,
            lambda: quote_client.get_option_bars(
                identifiers=[identifier], limit=1, market=Market.US
            ),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_option_timeline",
            OPTION_TIMELINE_LIMITER,
            lambda: quote_client.get_option_timeline(
                identifiers=[identifier], market=Market.US
            ),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_option_analysis",
            OPTION_ANALYSIS_LIMITER,
            lambda: quote_client.get_option_analysis(
                symbols=[TEST_SYMBOL], market=Market.US
            ),
            timeout_seconds,
        )
    )

    return results


def _capture_expiry(quote_client, context: ProbeContext) -> None:
    """Record the soonest expiry that has not already passed.

    Tiger returns past dates in the expirations list, so the first row is not
    necessarily tradable. Using an expired date would make later probes fail
    for a reason that has nothing to do with capability.

    Args:
        quote_client: A tigeropen QuoteClient.
        context: Updated in place with the chosen expiry.
    """
    from api.service.market import MarketDataError, list_expirations

    try:
        expiries = list_expirations(quote_client, TEST_SYMBOL)
    except MarketDataError:
        return
    except Exception:
        return

    for expiry in expiries:
        if expiry.days_to_expiry >= 0:
            context.expiry_date_text = expiry.date_text
            return


def _capture_identifier(
    quote_client,
    context: ProbeContext,
    chain_result: ProbeResult,
    expiry_for_chain: str,
) -> None:
    """Get a contract identifier for the identifier-based probes.

    Prefers a real one from the chain. Falls back to building one with the
    SDK's own helper -- never with string formatting, which is the classic way
    to produce an identifier that looks right and does not exist.

    Args:
        quote_client: A tigeropen QuoteClient.
        context: Updated in place.
        chain_result: The outcome of the chain probe.
        expiry_for_chain: The expiry used for the chain call.
    """
    from tigeropen.common.consts import Market
    from tigeropen.common.util.contract_utils import get_option_identifier

    if chain_result.outcome == OK:
        try:
            CHAIN_LIMITER.wait()
            chain_frame = quote_client.get_option_chain(
                symbol=TEST_SYMBOL, expiry=expiry_for_chain, market=Market.US
            )
            if chain_frame is not None and not chain_frame.empty:
                context.option_identifier = str(chain_frame.iloc[0]["identifier"])
                context.identifier_is_real = True
                return
        except Exception:
            pass

    expiry_compact = expiry_for_chain.replace("-", "")
    context.option_identifier = get_option_identifier(
        TEST_SYMBOL, expiry_compact, "CALL", 250
    )
    context.identifier_is_real = False
    context.notes.append(
        "The chain was unreachable, so the identifier-based option probes used a "
        "contract built with the SDK helper get_option_identifier "
        f"({context.option_identifier.strip()}). That contract may not exist, so an "
        "ERROR on those rows is inconclusive -- a DENIED result is still meaningful."
    )


def probe_stock_endpoints(quote_client, timeout_seconds: int) -> list[ProbeResult]:
    """Probe the stock market data endpoints.

    Args:
        quote_client: A tigeropen QuoteClient.
        timeout_seconds: Per-call timeout.

    Returns:
        One result per endpoint.
    """
    from tigeropen.common.consts import Market

    category = "Stock market data"
    results = []

    results.append(
        run_one_probe(
            category,
            "get_stock_briefs",
            STOCK_BRIEFS_LIMITER,
            lambda: quote_client.get_stock_briefs(symbols=[TEST_SYMBOL]),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_stock_delay_briefs",
            DELAYED_STOCK_BRIEFS_LIMITER,
            lambda: quote_client.get_stock_delay_briefs(symbols=[TEST_SYMBOL]),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_bars",
            BARS_LIMITER,
            lambda: quote_client.get_bars(symbols=[TEST_SYMBOL], limit=1),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_trade_ticks",
            TRADE_TICKS_LIMITER,
            lambda: quote_client.get_trade_ticks(
                symbols=[TEST_SYMBOL], begin_index=-1, end_index=-1, limit=1
            ),
            timeout_seconds,
        )
    )
    results.append(
        run_one_probe(
            category,
            "get_market_status",
            MARKET_STATUS_LIMITER,
            lambda: quote_client.get_market_status(market=Market.US),
            timeout_seconds,
        )
    )

    return results


class Report:
    """Collects the report so it can be printed and written to a file at once."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str = "") -> None:
        """Add one line to the report and print it.

        Args:
            text: The line.
        """
        self.lines.append(text)
        print(text)

    def write_to(self, path: Path) -> None:
        """Write the collected report to a file.

        Args:
            path: Where to write.
        """
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def add_results_table(report: Report, results: list[ProbeResult]) -> None:
    """Add the grouped results table to the report.

    Args:
        report: The report being built.
        results: Every probe result, in run order.
    """
    categories: list[str] = []
    for result in results:
        if result.category not in categories:
            categories.append(result.category)

    for category in categories:
        report.add(category.upper())
        report.add("-" * RULE_WIDTH)

        for result in results:
            if result.category != category:
                continue

            detail = result.detail
            if result.outcome == DENIED and result.entitlement:
                detail = f"needs: {result.entitlement}"

            report.add(f"  {result.outcome:<8} {result.endpoint:<26} {detail}")

        report.add("-" * RULE_WIDTH)
        report.add()


def add_permission_detail(report: Report, permissions: Any) -> None:
    """List the market data permissions the account actually holds.

    Args:
        report: The report being built.
        permissions: The value returned by get_quote_permission.
    """
    report.add("MARKET DATA PERMISSIONS HELD")
    report.add("-" * RULE_WIDTH)

    if looks_empty(permissions):
        report.add("  None. Only free endpoints will work.")
        report.add("-" * RULE_WIDTH)
        report.add()
        return

    for entry in permissions:
        if not isinstance(entry, dict):
            report.add(f"  {entry}")
            continue

        name = str(entry.get("name", "?"))
        description = PERMISSION_DESCRIPTIONS.get(name, "")
        expires_at = entry.get("expire_at")

        if expires_at == -1:
            expiry_text = "permanent"
        elif isinstance(expires_at, int):
            expiry_text = f"expires {date.fromtimestamp(expires_at / 1000)}"
        else:
            expiry_text = ""

        report.add(f"  {name:<28} {description:<34} {expiry_text}")

    report.add("-" * RULE_WIDTH)
    report.add()


def add_summary(report: Report, results: list[ProbeResult], context: ProbeContext) -> None:
    """Add the plain-English summary.

    Args:
        report: The report being built.
        results: Every probe result.
        context: Notes gathered during the run.
    """
    working = [result for result in results if result.outcome == OK]
    denied = [result for result in results if result.outcome == DENIED]
    errored = [result for result in results if result.outcome == ERROR]

    report.add("SUMMARY")
    report.add("=" * RULE_WIDTH)
    report.add()

    report.add(f"WHAT YOU CAN DO TODAY ({len(working)} endpoints)")
    if working:
        for result in working:
            report.add(f"  - {result.endpoint}  ({result.detail})")
    else:
        report.add("  Nothing responded. Check credentials before reading anything else.")
    report.add()

    report.add(f"WHAT IS BLOCKED ({len(denied)} endpoints)")
    if not denied:
        report.add("  Nothing. Every endpoint probed was reachable.")
    else:
        entitlements_needed: dict[str, list[str]] = {}
        for result in denied:
            key = result.entitlement or "unspecified"
            entitlements_needed.setdefault(key, []).append(result.endpoint)

        for entitlement, endpoints in entitlements_needed.items():
            permission_key = MARKET_TO_PERMISSION_KEY.get(entitlement, "")
            if permission_key:
                description = PERMISSION_DESCRIPTIONS.get(permission_key, "")
                report.add(
                    f"  Missing: {entitlement} market data "
                    f"-> buy '{permission_key}' ({description})"
                )
            else:
                report.add(f"  Missing entitlement: {entitlement}")
            for endpoint in endpoints:
                report.add(f"    - {endpoint}")
            report.add()

        report.add("  These are purchases, not bugs. Real-time OpenAPI market data is")
        report.add("  bought separately from a developer account, via the Tiger Trade app")
        report.add("  (My > market data access > OpenAPI Permissions) or Personal Center.")
        report.add("  US option data is a separate purchase from US stock data.")
    report.add()

    report.add(f"WHAT ERRORED FOR A NON-PERMISSION REASON ({len(errored)} endpoints)")
    if not errored:
        report.add("  Nothing. No unexpected failures.")
    else:
        for result in errored:
            report.add(f"  - {result.endpoint}: {result.detail}")
        report.add()
        report.add("  These are worth investigating. A permission problem reads as DENIED;")
        report.add("  anything here failed for some other reason.")
    report.add()

    for note in context.notes:
        report.add(f"NOTE: {note}")
        report.add()


def main(argv: list[str] | None = None) -> int:
    """Run the capability probe.

    Args:
        argv: Command line arguments, or None to read them from sys.argv.

    Returns:
        A process exit code. 0 unless the probe could not run at all; a DENIED
        result is a normal outcome and does not change the exit code.
    """
    parser = argparse.ArgumentParser(
        description="Report which Tiger API endpoints this account can reach.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait per call (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--grab",
        action="store_true",
        help=(
            "claim market data device access. Off by default: claiming it takes "
            "primary-device status away from whatever held it, such as the Tiger "
            "app on your phone. Pass this only if a real-time call was refused "
            "with 'current device does not have permission'."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="print full tracebacks")
    arguments = parser.parse_args(argv)

    try:
        settings = load_settings()
    except LiveTradingBlocked as error:
        print("BLOCKED BY THE SAFETY GUARD")
        print(error)
        return 2
    except ConfigError as error:
        print("CONFIGURATION PROBLEM")
        print(error)
        return 2

    try:
        print_startup_banner(settings.masked_account, settings.mode, settings.dry_run)
        print()

        trade_client = build_trade_client(settings)
        quote_client = build_quote_client(settings, grab_permission=arguments.grab)
    except LiveTradingBlocked as error:
        print("BLOCKED BY THE SAFETY GUARD")
        print(error)
        return 2
    except ClientSetupError as error:
        print("COULD NOT BUILD THE TIGER CLIENT")
        print(error)
        if arguments.debug:
            traceback.print_exc()
        return 3

    report = Report()
    context = ProbeContext()

    report.add("=" * RULE_WIDTH)
    report.add(f"  TIGER API CAPABILITY REPORT  --  {date.today().isoformat()}")
    report.add(f"  Account : {settings.masked_account}   Mode: {settings.mode}")
    report.add("  Read-only. No orders placed, constructed, or modified.")
    if arguments.grab:
        report.add("  Market data device access WAS claimed on connect (--grab).")
    else:
        report.add("  Market data device access was not claimed (project default).")
    report.add("=" * RULE_WIDTH)
    report.add()

    results: list[ProbeResult] = []
    results.extend(probe_account_endpoints(trade_client, arguments.timeout))

    permission_results = probe_permission_endpoints(quote_client, arguments.timeout)
    results.extend(permission_results)

    results.extend(probe_option_endpoints(quote_client, context, arguments.timeout))
    results.extend(probe_stock_endpoints(quote_client, arguments.timeout))

    add_results_table(report, results)

    # Fetch the held permissions once more for the detail block, only if the
    # endpoint worked. This is the one call worth making twice: it turns a list
    # of refusals into a list of what to buy.
    for result in permission_results:
        if result.endpoint == "get_quote_permission" and result.outcome == OK:
            try:
                QUOTE_PERMISSION_LIMITER.wait()
                add_permission_detail(report, quote_client.get_quote_permission())
            except Exception:
                pass

    add_summary(report, results, context)

    # The filename records which device-access default the run used, so two
    # reports are never compared across a difference they do not describe.
    grab_suffix = "grab" if arguments.grab else "nograb"
    output_path = Path(__file__).resolve().parent.parent / (
        f"capabilities-{date.today().isoformat()}-{grab_suffix}.txt"
    )
    report.write_to(output_path)

    print(f"Written to {output_path.name}")
    print("Diff two of these to see exactly what a purchase changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
