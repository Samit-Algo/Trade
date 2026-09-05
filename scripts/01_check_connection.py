"""Phase 1 -- prove the credentials work and prove which account we are on.

Read-only. This script queries the account and prints what Tiger reports back.
Confirm with your own eyes that it says PAPER before going any further.

    python scripts/01_check_connection.py
    python scripts/01_check_connection.py --debug     # show tracebacks

Documented calls used here:
  TradeClient.get_managed_accounts(account=None, lang=None)
  TradeClient.get_prime_assets(account=None, base_currency=None,
                               consolidated=True, lang=None)
https://docs-en.itigerup.com/docs/accounts
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.core.broker import ClientSetupError, build_trade_client  # noqa: E402
from api.service.core.config import ConfigError, Settings, load_settings  # noqa: E402
from api.service.core.safety import (  # noqa: E402
    LiveTradingBlocked,
    mask_account,
    print_startup_banner,
)

RULE = "-" * 60

#: Securities segment. Options live here, not in 'C' (futures) or 'F' (fund).
SECURITIES_SEGMENT = "S"


def _field(obj: object, name: str) -> object:
    """Read an optional attribute, tolerating SDK objects that omit it."""
    value = getattr(obj, name, None)
    return value if value is not None else "(not reported)"


def _print_account_profile(trade_client, settings: Settings) -> str | None:
    """Print what Tiger says about the configured account. Returns its type."""
    profiles = trade_client.get_managed_accounts()

    if not profiles:
        print("Tiger returned no managed accounts for this tiger_id.")
        print("Check that OpenAPI access is active and the account is funded.")
        return None

    match = next((p for p in profiles if getattr(p, "account", None) == settings.account), None)

    print("ACCOUNT AS REPORTED BY TIGER")
    print(RULE)
    if match is None:
        print(f"  Configured account   : {settings.masked_account}")
        print("  *** This account was NOT in the list Tiger returned. ***")
        print(f"  Tiger returned {len(profiles)} account(s): "
              + ", ".join(mask_account(getattr(p, 'account', '')) for p in profiles))
        print("  Check TIGER_ACCOUNT in .env character by character.")
        print(RULE)
        return None

    account_type = getattr(match, "account_type", None)
    print(f"  Account ID (masked)  : {mask_account(getattr(match, 'account', ''))}")
    print(f"  Account type         : {_field(match, 'account_type')}")
    print(f"  Capability           : {_field(match, 'capability')}")
    print(f"  Status               : {_field(match, 'status')}")
    print(RULE)

    if account_type == "PAPER":
        print("  Tiger confirms this is a PAPER TRADING account.")
    elif account_type is None:
        print("  Tiger did not report an account type. Do not proceed until you")
        print("  have confirmed on the developer page which account this is.")
    else:
        print(f"  *** WARNING: Tiger reports account_type={account_type!r}, not PAPER. ***")
        print("  *** Stop. Verify this account ID on the Tiger developer page. ***")
    print(RULE)
    print()
    return account_type


def _print_assets(trade_client, settings: Settings) -> None:
    """Print currency and available funds for the securities segment."""
    portfolio = trade_client.get_prime_assets(base_currency="USD")

    segments = getattr(portfolio, "segments", None) or {}
    segment = segments.get(SECURITIES_SEGMENT)

    print("FUNDS")
    print(RULE)
    if segment is None:
        print(f"  No '{SECURITIES_SEGMENT}' (securities) segment was returned.")
        print(f"  Segments present: {', '.join(sorted(segments)) or '(none)'}")
        print(RULE)
        print()
        return

    print(f"  Currency             : {_field(segment, 'currency')}")
    print(f"  Available for trade  : {_field(segment, 'cash_available_for_trade')}")
    print(f"  Cash balance         : {_field(segment, 'cash_balance')}")
    print(f"  Net liquidation      : {_field(segment, 'net_liquidation')}")
    print(f"  Buying power         : {_field(segment, 'buying_power')}")
    print(RULE)

    currency_assets = getattr(segment, "currency_assets", None) or {}
    if currency_assets:
        print("  By currency:")
        for code in sorted(currency_assets):
            asset = currency_assets[code]
            print(f"    {code:<5} cash {_field(asset, 'cash_balance')}"
                  f"  |  available {_field(asset, 'cash_available_for_trade')}")
        print(RULE)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 -- connect and confirm the account.")
    parser.add_argument("--debug", action="store_true",
                        help="print full tracebacks instead of a plain message")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except LiveTradingBlocked as exc:
        print("BLOCKED BY THE SAFETY GUARD")
        print(RULE)
        print(exc)
        print(RULE)
        return 2
    except ConfigError as exc:
        print("CONFIGURATION PROBLEM")
        print(RULE)
        print(exc)
        print(RULE)
        return 2

    try:
        # The banner comes before anything else, including client construction.
        print_startup_banner(settings.masked_account, settings.mode, settings.dry_run)
        print()

        trade_client = build_trade_client(settings)
        _print_account_profile(trade_client, settings)
        _print_assets(trade_client, settings)

    except LiveTradingBlocked as exc:
        print()
        print("BLOCKED BY THE SAFETY GUARD")
        print(RULE)
        print(exc)
        print(RULE)
        return 2
    except ClientSetupError as exc:
        print()
        print("COULD NOT BUILD THE TIGER CLIENT")
        print(RULE)
        print(exc)
        print(RULE)
        if args.debug:
            traceback.print_exc()
        return 3
    except Exception as exc:  # noqa: BLE001 - top-level handler, message first
        print()
        print("THE ACCOUNT QUERY FAILED")
        print(RULE)
        print(f"{type(exc).__name__}: {exc}")
        print()
        print("Common causes, in the order worth checking:")
        print("  1. Private key format -- the Python SDK expects PKCS#8.")
        print("  2. TIGER_ID does not match the developer page.")
        print("  3. OpenAPI access is not active, or the account is not funded.")
        print("  4. TIGER_LICENSE is unset but your account requires one")
        print("     (TBHK licences also need tiger_openapi_token.properties).")
        print("  5. An IP whitelist on the developer page excludes this machine.")
        print(RULE)
        print("Re-run with --debug for the full traceback.")
        if args.debug:
            print()
            traceback.print_exc()
        return 1

    print("Phase 1 complete. Read the account type above before going further.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
