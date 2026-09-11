# How this project is put together

**Read this first.** Two minutes here saves an hour of hunting.

---

## The one idea

There is **one folder with all the logic in it**, and **two ways to reach it**.

```
   You, typing a command              A program, sending HTTP
            │                                    │
            ▼                                    ▼
      scripts/*.py                        api/routes/*.py
      (command line)                      (web endpoints)
            │                                    │
            └──────────────┬─────────────────────┘
                           ▼
                    api/service/**
        ALL the logic. ALL four safety locks.
        Nothing is decided anywhere else.
                           │
                           ▼
                  Tiger Brokers' servers
```

**Why it matters:** to change *what the system does*, you change something in
`api/service/`. To change *how you ask it*, you change a script or a route.
You should almost never change both for one task.

A route handler is allowed to do exactly three things: check the request, call
the service, shape the reply. If you find yourself writing an `if` about money
in a route, it belongs in `api/service/` instead.

---

## `api/service/` — one folder per subject

Each subject is a folder. Each file in it answers one question.

```
api/service/
│
├── core/          The foundations. Nothing here knows what an option is.
│     safety.py      Am I allowed to trade?          ← the three locks
│     config.py      Where am I pointed?
│     broker.py      How do I reach Tiger, without going too fast?
│     audit.py       What did I do, and when?
│
├── market/        What exists out there, and what it is worth.
│     read_data.py   Reading Tiger's dataframes without crashing
│     calendar.py    What expiries exist, and the date maths
│     prices.py      Underlying price, last traded close, spread, liquidity
│     quotes.py      THE SEAM — where bid and ask come from
│
├── contract/      Which exact contract are we talking about?
│     errors.py      The four ways resolution can fail
│     identifiers.py Turning a contract into a string, and back
│     resolve.py     Does it exist? Is it expired? What is its ID?
│     selection.py   Which one, when the caller named none?
│
├── order/         Everything about an order.
│     cost.py        What will this cost me?        pure maths, no network
│     build.py       Build it, and show a human first
│     bracket.py     Take-profit and stop-loss legs
│     status.py      Status, fills, cancelling
│     ticks.py       The price grid. Measured, not assumed.
│     submit.py      THE ONLY FILE THAT CAN SPEND MONEY
│
└── position/      What is held, and how it is doing.
      holdings.py    What the account actually holds
      valuation.py   P&L at the bid, and the expiry warning
```

Every folder has an `__init__.py` that re-exports its public names, so you
import from the **folder**, not the file:

```python
from api.service.order import buy_option, estimate_cost
from api.service.contract import find_option_contract
from api.service.core.config import load_settings
```

`core/` is the exception — you name the file, because `core.safety` and
`core.config` are more informative than a bare `core`.

---

## The two files that are shaped on purpose

**`core/safety.py` stays small and alone**, at 131 lines. It holds the three
locks and nothing else. "The whole safety system is 131 lines, go read it" is
a claim you can act on. Burying it in `config.py` would save one file and cost
you that.

**`order/submit.py` is the entire spend path**, and nothing else is in it.
Every `place_order` call in the project is in that one file, each between two
`assert_order_allowed` gates. Cancelling lives next door in `status.py`,
because cancelling cannot open a position. If you are reviewing whether this
project can lose money by accident, `submit.py` is the file to read.

---

## Where do I go to change X?

| I want to... | Open this |
|---|---|
| Change what stops a bad order | `service/core/safety.py` |
| Add or rename a `.env` setting | `service/core/config.py` |
| Change how we connect, or a rate limit | `service/core/broker.py` |
| Change what gets written to the order log | `service/core/audit.py` |
| Change how a quote is entered by hand | `service/market/quotes.py` |
| Change expirations or the date maths | `service/market/calendar.py` |
| Change the underlying or last-traded lookup | `service/market/prices.py` |
| Change how a contract is found or validated | `service/contract/resolve.py` |
| Change cost, break-even, or commission maths | `service/order/cost.py` |
| Change what the preview prints | `service/order/build.py` |
| Change the take-profit / stop-loss rules | `service/order/bracket.py` |
| Change polling, fills, or cancelling | `service/order/status.py` |
| Change the submission path itself | `service/order/submit.py` |
| Change position P&L or the expiry warning | `service/position/valuation.py` |
| Add or change an HTTP endpoint | `api/routes/` |
| Change a request or response shape | `api/schemas.py` |
| Change an HTTP error code | `api/errors.py` |

---

## The HTTP door: `api/`

| File | Job |
|---|---|
| `main.py` | The server. API key check, error handlers, startup. |
| `errors.py` | One table: which exception becomes which HTTP status. |
| `shared.py` | Builds settings and clients once. Request logging. |
| `schemas.py` | Every request/response shape, and how to build one. |
| `order_rules.py` | Idempotency keys, and the price-only quote. |
| `routes/health.py` | `GET /health` |
| `routes/market.py` | `GET /expirations/{underlying}` |
| `routes/positions.py` | `GET /positions`, `GET /positions/detail` |
| `routes/orders.py` | history, status, legs — **all read-only** |
| `routes/trade.py` | `POST /trade` — one call, one bracketed BUY — and `GET /trade/settings` |
| `routes/ui.py` | `GET /ui` — a hand-testing form for `/trade` |

**`main.py` is the root of the import graph.** It imports everything; nothing
imports it. That is why `errors.py` and the request logger live outside it —
the low-level modules need them, and `main.py` already imports those modules.
Putting them in `main.py` creates a circular import, which is exactly what
happened the first time and is why they are where they are.

---

## The command-line door: `scripts/`

Five scripts. Each one is the evidence behind a finding in `HANDOVER.md`.

| Script | What it proves |
|---|---|
| `00_check_capabilities.py` | Which Tiger endpoints this account can reach |
| `01_check_connection.py` | The credentials work and this is the PAPER account |
| `03_find_contract.py` | A contract resolves, and a bad one is refused properly |
| `05_paper_order.py` | **The only script that can place an order** |
| `06_positions.py` | What is held, valued at the bid |

---

## Following one request all the way through

Placing an order via HTTP. There is one way to do it:

```
POST /trade   { client_order_id, symbol, current_price, option_type }
      api/main.py                    checks X-API-Key            ← Lock 0
      api/routes/trade.py            claims the client_order_id
      service/contract/resolve.py    which contract? (cached)
      service/market/prices.py       last traded price, free bars
      service/order/ticks.py         snap to the grid, add the buffer
      service/order/bracket.py       the take-profit and stop-loss
      service/order/cost.py          what does it cost?
   ── if DRY_RUN is true, it returns HERE with order_id: null ──
      service/order/submit.py
            build the order          (service/order/build.py)
            assert_order_allowed()   ← the guard, right before the wire
            place_order()            ← the only one in the codebase
      service/order/status.py        one status read, no sleeping
      service/core/audit.py          writes the record
   → the order, and every number behind it
```

**Why no confirmation step?** There used to be one: a two-step
preview-then-submit with a single-use token, standing in for the cash amount a
human types at the CLI. It was removed with the rest of the unused surface. What
replaces it is `DRY_RUN`, which is a deployment-level decision rather than a
per-request one, plus the `client_order_id` — claimed *before* the order can
reach the broker, so a retry replays the first outcome instead of buying twice.

**Why `assert_order_allowed` twice?** The first is the gate, before anyone is
asked to confirm anything. The second sits immediately before the wire, so
nothing in between could have changed the mode or cleared the dry-run flag.

---

## The four locks

An order reaches Tiger only if all four are satisfied. Three are in the
service layer and apply everywhere; the fourth is HTTP-only.

| Lock | Where | Default |
|---|---|---|
| 0 — API key | `api/main.py` | no key set → the service will not start |
| 1 — Account allowlist | `service/core/safety.py` | fails closed |
| 2 — Live opt-in | `service/core/safety.py` | `false` |
| 3 — Dry run | `service/core/safety.py` | `true` — nothing can be sent |

With the shipped defaults, **the system physically cannot place an order.**
That is intentional.

---

## Three greps that prove it still holds

```bash
# 1. The submission call exists in ONE file, reached from two places.
grep -rn 'trade_client\.place_order(' --include='*.py' api scripts
#    expect 2 hits, both in api/service/order/submit.py

# 2. Both guards still present on both order paths, in that same file.
grep -c '^[[:space:]]*assert_order_allowed(' api/service/order/submit.py
#    expect 4
grep -rl '^[[:space:]]*assert_order_allowed(' --include='*.py' api scripts
#    expect api/service/order/submit.py, and nothing else

# 3. Nothing outside quotes.py names a concrete quote provider.
grep -rn 'ManualEntryProvider\|TigerQuoteProvider' --include='*.py' api scripts \
  | grep -v 'market/quotes.py'
#    expect nothing at all
```

The third grep deliberately covers `api` and `scripts` but not `tests` —
`tests/test_providers.py` is the test *of* the seam, so it names the class on
purpose and says so in a comment.

---

## Where to go next

- **`README.md`** — how to run it, and the commission warning worth reading
  before you trade
- **`HANDOVER.md`** — the full reference: every finding, every SDK gotcha,
  what still needs buying
- **`SPEC-ADDENDUM-manual-market-data.md`** — why quotes are typed by hand
