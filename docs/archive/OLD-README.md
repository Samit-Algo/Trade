# Tiger Options Backend

A Python backend that talks to the Tiger Brokers OpenAPI, built in deliberate
stages so that no code capable of spending real money exists until the final
phase, and even then it is locked behind two independent switches.

**Current state: all six spec phases complete, plus Phase 7 (attached orders),
Phase 8 (HTTP API) and Phase 9 (simplification).** Eight real orders have been
placed on the paper account. Every script except `05_paper_order.py` is
read-only.

> **New to this code? Read [ARCHITECTURE.md](ARCHITECTURE.md) first.** One
> page, two minutes: where everything lives and why.
>
> Resuming work after a break? Read **[HANDOVER.md](HANDOVER.md)**. It records
> what is verified and how, the one entitlement still worth buying, the
> confirmed SDK signatures, the sixteen documented gotchas, and the environment
> facts that are not in the repo.

---

## Where the code lives

```
api/
  main.py       the HTTP server        routes/    the endpoints
  service/     ALL the logic, one folder per subject:
    core/        connect, configure, and the three safety locks
    market/      expiries, prices, and where bid/ask come from
    contract/    which exact contract
    order/       cost it, build it, send it, track it
    position/    what is held, and the P&L
scripts/       five command-line tools, the same logic, no HTTP
tests/         236 tests, all offline
```

The rule: to change **what the system does**, change something in
`api/service/`. To change **how you ask it**, change a route or a script. You
should almost never change both for one task. `ARCHITECTURE.md` has the file
-by-file map and a "I want to change X, open file Y" table.

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

`api/service/core/safety.py` holds these guards. `assert_order_allowed()` also
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
python scripts/00_check_capabilities.py AAPL     # lists the real expirations
curl -H "X-API-Key: $KEY" localhost:8000/expirations/AAPL
```

Expiration dates come back with days-to-expiry and a weekly/monthly tag. This
part is free and works today.

**The chain table does not.** A CALLS | STRIKE | PUTS display was built and
verified against a synthetic fixture, but `get_option_chain` needs the US
option market-data entitlement, so it never ran against live data. The script
was removed in Phase 9 rather than left as a thing that always fails —
`git log` has it, and `HANDOVER.md` §3c says how to bring it back the day the
entitlement is bought.

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
| `get_option_bars`, `get_option_timeline` | No — free |
| contract lookup (`get_contract`, `get_derivative_contracts`) | No — free |

Real-time OpenAPI market data is purchased separately from the Tiger Trade app
or Personal Center; it is not included with a developer account. The underlying
price falls back to the free delayed feed automatically and says which one you
got. The option chain has no free fallback — Tiger publishes no delayed option
endpoint — so nothing here can print a chain until US option market data is
active on the account.

---

## Phases 3 to 6

```bash
python scripts/03_find_contract.py AAPL 2026-09-18 320 CALL
python scripts/05_paper_order.py   AAPL 2026-09-18 360 CALL BUY 1
python scripts/05_paper_order.py --status 44506652990393344
python scripts/06_positions.py
```

**Phase 3** resolves a human request into exactly one verified contract. Tiger
itself refuses an impossible strike, so validation is the exchange's answer
rather than a local guess. An expiry that is listed but already past is
refused as **expired**, not as "not found".

**Phase 4** costs the order and sends nothing. Its own script was removed in
Phase 9, and `POST /orders/preview` in Phase 11; the preview step inside
`05_paper_order.py` is what remains. Quotes are typed by hand from
the Tiger app behind a `MarketDataProvider` seam, labelled `[MANUAL]`
everywhere, stale after 60 seconds, and checked against the contract's last
traded price — a price more than 3x or less than 0.33x that must be retyped
inside an override phrase a reflexive `y` cannot clear.

**Phase 5** submits, to the paper account only, behind the three locks and a
typed confirmation of the cash amount. An order ID confirms **submission, not
execution**, so it polls afterwards and reports what actually filled. Outcomes
come from `filled` and `avg_fill_price`, never from what was requested — an
order marked CANCELLED or EXPIRED may still have filled in part.

**Phase 6** values positions at the **bid**, because a position is worth what
someone will pay for it. Tiger's own `unrealized_pnl` uses `latestPrice` and
is shown alongside for comparison, not used. Positions near expiry warn about
the cash an automatic exercise would need — $36,000 for one AAPL 360 call.

---

## Phase 7 — attached take-profit and stop-loss

```bash
python scripts/05_paper_order.py AAPL 2026-09-18 360 CALL BUY 1     --take-profit 0.60 --stop-loss 0.15
python scripts/05_paper_order.py AAPL 2026-09-18 370 CALL BUY 1     --take-profit 0.40 --stop-loss 0.07 --leg-tif GTC
python scripts/05_paper_order.py --legs 44506905057837056
```

Legs attach to a **parent order** and activate when it fills. They **cannot be
attached to a position you already hold** — to bracket an existing holding you
must close it and buy again with legs attached.

Five things that had to be discovered by placing real orders, because the
documentation does not say:

- **Attached legs work on options.** Every doc example uses a stock.
- **Both a take-profit and a stop-loss attach to one parent.** Tiger's app help
  says one sub-order; that does not describe the API. The SDK sends both as
  `attach_type='BRACKETS'`.
- **`GTC` works on a leg** even though a paper account rejects it on the parent.
  Confirmed as *stored*, not merely accepted, with a `DAY` control on a second
  order — a silent downgrade would be worse than a rejection.
- **The legs appear as child orders carrying `parent_id`**, not on the parent's
  `order_legs` attribute, which stayed empty. Checking only that attribute
  would suggest the legs were never created.
- **A stop leg carries its price in `aux_price`** and becomes order type `STP`;
  a take-profit uses `limit_price` and becomes `LMT`. Reading the wrong field
  returns `None`.

> **A bracketed order cannot be checked before it is sent.** `preview_order`
> refuses attached orders outright — `code=1010 OCA/ATTACHED order preview not
> supported` — for options and stocks alike, while previewing a plain option
> order fine. The local checks are the only pre-submission check that exists,
> which is why the preview warns loudly when a take-profit sits below
> break-even.

---

## Phase 8 — HTTP API

```bash
python -m api.main             # binds 127.0.0.1:8000 by default
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/health
```

A **second entry point over the same library**, not a rewrite. Every route
calls the functions the CLI scripts call; no business logic lives in a
handler, and `scripts/` keeps working unchanged.

### The fourth lock

An HTTP port that can place orders is a different risk from a CLI, so:

- every request needs a matching `X-API-Key` header, or **401** before routing
- the service **refuses to start** without `TIGER_API_KEY` set
- it binds to **127.0.0.1**, not `0.0.0.0`
- `POST /trade` places nothing when `DRY_RUN` is true — it returns every price
  with `order_id: null` — and every order request is logged with its client IP

Locks 1 and 2 resolve at startup; `assert_order_allowed` runs immediately
before `place_order`, the last thing that happens before the wire.

### There is one way to place an order

```
POST /trade   { client_order_id, symbol, current_price, option_type }
```

Four fields. Quantity, the take-profit and stop-loss percentages, the expiry,
the strike distance and the leg time-in-force all come from `.env` and are
validated at startup — see the Phase 11 block in `.env.example`. `GET
/trade/settings` reports what is configured, so a client can show it.

`DRY_RUN` decides whether anything is sent:

| `DRY_RUN` | What happens |
|---|---|
| `true` | Every price is worked out and returned; `order_id: null`, `order_status: "NOT_SUBMITTED"` |
| `false` | The same work, then the bracketed order goes to the broker |

**`POST /trade` is safe to retry.** The `client_order_id` is claimed *before*
the order can reach the broker, so a client that times out and resends gets the
first outcome replayed with `duplicate: true`, never a second position.

### The endpoints

Everything else is read-only.

| Endpoint | What it is for |
|---|---|
| `GET /health` | mode, dry-run state, account, whether orders are live |
| `GET /trade/settings` | the `.env` decisions `POST /trade` will apply |
| `GET /positions` | what is held, valued at the bid |
| `GET /positions/detail` | one position, with its working orders |
| `GET /expirations/{underlying}` | which expiries are listed |
| `GET /orders/history` | recent orders |
| `GET /orders/{id}` | one order's fill state |
| `GET /orders/{id}/legs` | its attached take-profit and stop-loss |
| `GET /ui` | the hand-testing form |

### Errors: coarse status, precise code

Branch on `error_code`, never on the message. Notably **410 Gone** for an
expiry that is listed but has passed, distinct from **404** for one Tiger never
listed — the Phase 3 finding expressed in the protocol.

Interactive docs at `/docs`.

---

## Roadmap

Each phase must run cleanly against the paper account before the next begins.
Phase 7 was added after the spec was finished.

| Phase | Scope | State |
|---|---|---|
| 1 | Connect and confirm the account | **done** — Tiger confirmed PAPER, Funded, RegTMargin |
| 2 | Market data: expirations and chains | **done** — 24 real expirations; the chain *table* needs `usOptionQuote` |
| 3 | Contract resolution | **done** — valid CALL and PUT, bad strike, expired expiry |
| 4 | Cost estimation and simulated orders | **done** — BUY and SELL previews, decimal-slip override |
| 5 | Paper order submission | **done** — one real order filled, see below |
| 6 | Positions and P&L | **done** — position valued at the bid |
| 7 | Attached take-profit and stop-loss *(new, outside the spec)* | **done** — brackets filled with DAY and GTC legs |
| 8 | HTTP API *(new, outside the spec)* | **done** — FastAPI over the same library, four locks |

### The real paper order

```
AAPL  260918C00360000   BUY 1 @ LIMIT 0.30
Order ID 44506652990393344  ->  FILLED 1/1 at 0.2800
ACTUAL CASH $28.00 against a $30.00 estimate
```

Lock 3 was proved first: with `DRY_RUN=true` the flow blocked before the
confirmation prompt. It was set false for that one order and restored
immediately.

### One thing worth knowing before you trade

**Commission is a fixed toll of about $3.00 per order.** Measured across four
real orders at two sizes, both directions:

```
BUY  1 contract  $3.02      BUY  3 contracts  $3.09
SELL 1 contract  $3.02      SELL 3 contracts  $3.10
```

Neither flat (which predicts $3.02 for three, out by $0.07) nor per-contract
(which predicts $9.06, out by $5.97). It fits **`$2.985 + $0.035 × contracts`**,
and the base dominates — tripling the size added seven cents.

So what matters is **total premium**, not contract count. Holding the contract
price constant at $0.28/share so size is isolated from price:

| Contracts | Premium | Round-trip commission | % of premium |
|---:|---:|---:|---:|
| 1 | $28 | $6.04 | **21.6%** |
| 2 | $56 | $6.11 | 10.9% |
| 3 | $84 | $6.18 | 7.4% |
| 5 | $140 | $6.32 | 4.5% |
| 10 | $280 | $6.67 | 2.4% |
| 20 | $560 | $7.37 | 1.3% |

A $28 position is not a small trade, it is a bad one: it pays a 21.6% toll
before the market does anything, and needs a 21.6% move just to break even.
Demonstrated rather than argued — a round trip that bought at 0.28 and sold at
0.28 returned `realized_pnl -$6.04`, entirely commission.

Below **10%** of premium at about **$84**, below **5%** at **$140**, below
**2%** at **$364**.

**Rule of thumb: keep total premium above roughly $150 per position**, and
treat anything under about $85 as a trade the fee structure has already decided
against. HANDOVER.md §3 has the full arithmetic.

---

## Reference

Official documentation: <https://docs-en.itigerup.com/docs/>

Every API call used. All are read-only except the last line, which is
reachable only through `05_paper_order.py` and only past the three locks:

- `TradeClient.get_managed_accounts(account=None, lang=None)`
- `TradeClient.get_prime_assets(account=None, base_currency=None, consolidated=True, lang=None)`
- `QuoteClient.get_option_expirations(symbols, market=None)` — 60/min
- `QuoteClient.get_option_chain(symbol, expiry, option_filter=None, return_greek_value=None, market=None, timezone=None)` — 60/min
- `QuoteClient.get_option_briefs(identifiers, market=None, timezone=None)` — 120/min
- `QuoteClient.get_stock_briefs(symbols, include_hour_trading=False, lang=None)` — 120/min
- `QuoteClient.get_stock_delay_briefs(symbols, lang=None)` — 10/min
- `QuoteClient.get_option_bars(identifiers, ...)` — 60/min
- `TradeClient.get_contract(...)`, `get_derivative_contracts(...)` — 60/min
- `TradeClient.get_positions(...)` — 60/min
- `TradeClient.get_order(...)` — 120/min
- `TradeClient.place_order(order)`, `cancel_order(...)` — 120/min — **these write**

Every documented per-endpoint rate limit is enforced by
`api/service/core/broker.py`.

The full signature list, with every gotcha found while using them, is in
[HANDOVER.md](HANDOVER.md).
