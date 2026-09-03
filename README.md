# Tiger Options Backend

A Python backend that talks to the Tiger Brokers OpenAPI, built in deliberate
stages so that no code capable of spending real money exists until the final
phase, and even then it is locked behind two independent switches.

**Current state: Phases 1 and 2.** Read-only throughout.

> Resuming work after a break? Read **[HANDOVER.md](HANDOVER.md)** first.
> It records what is verified, the market data entitlement currently
> blocking the chain display, the confirmed SDK signatures, and the
> environment facts that are not in the repo.

---

## Risk statement

> This software can place real financial orders. Buying options carries a normal, expected
> outcome of losing 100% of the amount paid. An option can lose value even when the
> underlying moves in the predicted direction, because of time decay and falling implied
> volatility. An in-the-money option held to expiry is automatically exercised into a
> share transaction that may require many times the option's cost in cash. A mistyped
> quantity multiplies exposure by 100 per contract. Use the paper account until every
> path in this system has been exercised and understood.

---

## What this system deliberately does not do

- It does not decide what to trade. There is no strategy, no signal, no automation.
- It does not manage risk beyond displaying cost and maximum loss.
- It does not handle exercise or assignment.
- It does not support multi-leg or combo orders.
- It is not permitted to trade a live account in this build.

---

## Account safety architecture

The Tiger SDK treats a paper account and a live account identically. The *only*
thing distinguishing them is the account ID string in the config. A single wrong
character means real money.

An order can only reach Tiger if **all three** locks are satisfied:

| Lock | Mechanism | Default |
|---|---|---|
| **Lock 1 — Account allowlist** | The configured account ID must exactly match `TIGER_PAPER_ACCOUNT` in `.env` | fails closed |
| **Lock 2 — Live opt-in** | `TIGER_ALLOW_LIVE` must be `true` to permit any account not in the allowlist | `false` |
| **Lock 3 — Dry run** | `DRY_RUN` must be `false` for `place_order` to be called at all | `true` |

With the shipped defaults the system is physically incapable of placing an
order. That is intentional.

`tiger_backend/safety.py` holds these guards. `assert_order_allowed()` also
blocks LIVE outright. Removing that line is a separate, deliberate act you must
perform yourself — it is not part of this build.

---

## Setup

**1. Register for OpenAPI access.** Log in to the Tiger developer page and
activate OpenAPI. This requires a funded account.

**2. Generate an RSA key pair and save the private key immediately.** Tiger does
not store it, and it disappears when the page is refreshed. PKCS#8 is the format
recommended for the Python SDK. Store it at `./secrets/tiger_private_key.pem`.

> The private key is equivalent to a bank password. It must never be committed.
> `.env`, `secrets/` and `*.pem` are gitignored in the first commit of this repo.

**3. Find your paper trading account ID** on the developer page. It is listed
separately from your live account, and is 17 digits. Copy both IDs somewhere you
can compare them character by character.

**4. Fill in `.env`.**

```bash
cp .env.example .env
```

Put the **paper** account ID in both `TIGER_ACCOUNT` and `TIGER_PAPER_ACCOUNT`.
They must be identical for the system to run in PAPER mode.

**5. Install dependencies.** Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

---

## Phase 1 — connect and confirm the account

```bash
python scripts/01_check_connection.py
```

It prints the startup banner, then the account ID (masked to the last four
digits), the account type and status as Tiger reports them, the currency, and
the available funds.

**Verification step — do this before trusting anything.** Confirm with your own
eyes that it reports the **paper** account before continuing. Do not skip this.

Add `--debug` for full tracebacks; without it, failures print a plain-English
message rather than a stack trace.

### Tests

The Phase 1 logic that needs no network — account mode resolution, the locks,
account masking, config parsing — is unit-tested:

```bash
python -m pytest tests/ -q
```

These run without Tiger credentials and without the `tigeropen` SDK installed.

---

## Phase 2 — market data: expirations and chains

```bash
python scripts/02_show_chain.py AAPL
python scripts/02_show_chain.py AAPL --all           # every strike
python scripts/02_show_chain.py AAPL --strikes 5     # 5 either side of the money
python scripts/02_show_chain.py AAPL --expiry 2026-09-18
```

Lists the expiration dates Tiger reports, with days-to-expiry and a
weekly/monthly tag, lets you pick one, then prints a CALLS | STRIKE | PUTS
table with bid, ask, spread, volume, open interest and implied volatility.

By default it shows ten strikes either side of the money, with the
at-the-money strike marked `> ... <`. Rows whose volume or open interest is
below the threshold are flagged `!` — those have wide spreads and can be hard
to sell later.

**Expiry dates are never constructed.** Listed expiries are irregular, and a
date built from a calendar rule can look entirely plausible while not existing
as a contract — you find out when an order is rejected. Every date offered here
came from `get_option_expirations`.

### Greeks are deliberately not displayed

Tiger marks the option-chain Greek fields (`delta`, `gamma`, `theta`, `vega`,
`rho`) as **deprecated**. They update once a day and are not suitable for
intraday decisions. This project does not request them, does not display them,
and builds no logic on them. Tiger's guidance is to calculate Greeks locally
from current market inputs instead.

### Market data access

| Endpoint | Needs paid access? |
|---|---|
| `get_option_expirations` | No — free |
| `get_stock_delay_briefs` (delayed ~15 min) | No — free |
| `get_stock_briefs` (real-time) | **Yes** — US market data |
| `get_option_chain` | **Yes** — US **option** market data |
| `get_option_briefs` | **Yes** — US **option** market data |

Real-time OpenAPI market data is purchased separately from the Tiger Trade app
or Personal Center; it is not included with a developer account. The underlying
price falls back to the free delayed feed automatically and says which one you
got. The option chain has no free fallback — Tiger publishes no delayed option
endpoint — so `02_show_chain.py` cannot print a chain until US option market
data is active on the account.

---

## Roadmap

Each phase must run cleanly against the paper account before the next begins.

| Phase | Scope | State |
|---|---|---|
| 1 | Connect and confirm the account | **implemented** |
| 2 | Market data: expirations and chains | **implemented** (chain display needs US option market data) |
| 3 | Contract resolution | not started |
| 4 | Cost estimation and simulated orders | not started |
| 5 | Paper order submission | not started |
| 6 | Positions and P&L | not started |

---

## Reference

Official documentation: <https://docs-en.itigerup.com/docs/>

All API calls used so far are read-only:

- `TradeClient.get_managed_accounts(account=None, lang=None)`
- `TradeClient.get_prime_assets(account=None, base_currency=None, consolidated=True, lang=None)`
- `QuoteClient.get_option_expirations(symbols, market=None)` — 60/min
- `QuoteClient.get_option_chain(symbol, expiry, option_filter=None, return_greek_value=None, market=None, timezone=None)` — 60/min
- `QuoteClient.get_option_briefs(identifiers, market=None, timezone=None)` — 120/min
- `QuoteClient.get_stock_briefs(symbols, include_hour_trading=False, lang=None)` — 120/min
- `QuoteClient.get_stock_delay_briefs(symbols, lang=None)` — 10/min

Every documented per-endpoint rate limit is enforced by `tiger_backend/throttle.py`.
