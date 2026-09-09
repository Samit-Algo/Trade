# Handover

**All six spec phases complete, plus Phase 7 (attached orders), Phase 8
(HTTP API) and Phase 9 (simplification), 2026-09-04.** Everything below is
verified, not assumed.

> **New here? Read `ARCHITECTURE.md` first.** It is one page and explains the
> whole shape: two front doors, one brain, four locks. This file is the
> reference you come back to, not the place to start. Read this before touching the code so nothing gets re-derived.

The build specification is `../tiger-options-backend-spec.md`. It is the
authority; this file records what has actually been done against it.

`SPEC-ADDENDUM-manual-market-data.md` extends it: option quotes are typed in
by hand from the Tiger app instead of fetched, behind a provider interface.
Approved and implemented.

---

## 1. Phase status

| Phase | Scope | State | How it was verified |
|---|---|---|---|
| 1 | Connect and confirm the account | **done** | `scripts/01_check_connection.py` against the live paper account. Tiger reported `Account type: PAPER`, `Status: Funded`, `Capability: RegTMargin`, USD 1,000,000 available. |
| 2 | Market data: expirations and chains | **done, chain display blocked** | 24 real AAPL expirations returned live, with correct days-to-expiry and weekly/monthly tags. The chain **table** is verified against a realistic synthetic fixture only — the live chain needs `usOptionQuote`, see §2. |
| 3 | Contract resolution | **done** | Four cases live: a valid CALL and PUT resolved to real identifiers with contract IDs; strike 317.13 refused with the nearest real strikes listed; expiry 2026-09-02 refused as **EXPIRED**, naming 2026-09-04 as next tradable. |
| 4 | Cost estimation and simulated orders | **done** | BUY 1x AAPL 320 CALL → CASH REQUIRED $1,160.00, break-even $331.60. SELL 2x 300 PUT → CASH RECEIVED $110.00, max loss correctly refused as unbounded-if-opening. A deliberate decimal slip (ask 115.00 against an 11.21 last close) was blocked; `y` rejected, `USE 115.00` accepted. |
| 5 | Paper order submission | **done — a real order was placed** | See below. |
| 6 | Positions and P&L | **done** | The Phase 5 position read back and valued at a typed bid. See §3. |
| 7 | Attached take-profit and stop-loss (new, outside the spec) | **done** | Three bracketed orders placed live: DAY legs, GTC legs, and a 3-contract bracket whose legs were cancelled to settle the commission question. All filled; legs confirmed live and cancellable. See §3a. |
| 8 | HTTP API (new, outside the spec) | **done** | FastAPI over the same library. Health, 401 without a key, a full preview → submit cycle, a retried submit refused, a decimal-slip 400, a 403 under DRY_RUN, and GET/DELETE on a real order — all exercised live. See §3b. |
| 10 | Fast single-call trading API (new) | **done** | `POST /trade`: seven inputs, one bracketed BUY, one Tiger call. Tick size **measured** from 32,360 real traded prices, refuting the price-band convention the plan was going to hard-code. See §3d and §3e. |
| 9 | Simplification (new) | **done** | Reshaped for readability. `tiger_backend/` is gone; all logic now lives in `api/service/`, one folder per subject. The file that can spend money went from 1,548 lines to 427 and holds nothing else. Scripts trimmed to five, the orphaned chain-table feature removed, `ARCHITECTURE.md` added. All tests pass and every script was re-run live. See §3c. |

### The real paper order

```
Contract    AAPL  260918C00360000   (AAPL 2026-09-18 360.00 CALL)
Action      BUY 1 contract, LIMIT 0.30, time_in_force DAY, outside_rth False
Order ID    44506652990393344
Poll        1/12  status=FILLED  filled=1/1
Fill        avg fill price 0.2800
ACTUAL CASH $28.00   against a $30.00 estimate, difference -2.00
```

That −$2.00 gap is Phase 5 in miniature: the estimate was right about the
limit price and wrong about the fill. Only `filled` and `avg_fill_price` know
what actually happened.

**Lock 3 was proved before it was touched.** With `DRY_RUN=true` the flow
blocked at step 4, before the confirmation prompt was even offered. `DRY_RUN`
was set false for that one order and restored immediately afterwards, verified
blocking again. It is `true` now.

327 unit tests pass, all offline — no network, no credentials:

```bash
python -m pytest tests/ -q
```

Commits, newest first:

```
c761d32  Phase 8: FastAPI service over the existing library
a2c5ba5  Make the commission estimate exact; README current for seven phases
2ffd6fa  Settle the commission question: a fixed toll, not a per-contract fee
e9ad17f  Answer the GTC question; document Phase 7 in HANDOVER.md
8027e29  Phase 7: attached take-profit and stop-loss legs
f2a93f9  Bring HANDOVER.md and README current for all six phases
efe6e72  Phase 6: positions and P&L
98fa7ec  Phase 5: paper order submission
4c082ff  Phase 4: cost estimation, manual market data, simulated orders
36ed9f9  Phase 3: contract resolution
bbce43f  Add manual market data spec addendum; correct the entitlement picture
e8d6544  Stop claiming market data device access; add premium history viewer
1d5bfc3  Add scripts/00_check_capabilities.py, a read-only capability probe
1217e9f  Add HANDOVER.md
5fdead9  Fix volume column overflow, found by a realistic chain fixture
5bcfcb1  Phase 2: option expirations and chain display
fa704b0  Reject a properties file given as the private key path
9d93a39  Ignore vnv/ virtualenv directory
517f84a  Phase 1: connect and confirm the account
ee8a39e  Initial commit: project skeleton and .gitignore
```

---

## 2. What still needs buying

Only one thing: **`usOptionQuote`**, US option market data.

Without it there is no live chain display -- the script that printed one was
removed in Phase 9, recoverable from git -- and quotes are
typed by hand instead. Everything else works. Run
`python scripts/00_check_capabilities.py` to regenerate the picture; it writes
`capabilities-<date>-<grab|nograb>.txt` so two runs can be diffed across a
purchase. Both 2026-09-03 baselines are committed and differ only in their
header line.

| Endpoint | Result | Needs paid access |
|---|---|---|
| All account and trading queries | OK | No |
| All contract lookup (`get_contract`, `get_derivative_contracts`) | OK | No |
| `get_option_expirations`, `get_option_bars`, `get_option_timeline` | OK | No — free |
| `get_stock_delay_briefs`, `get_bars`, `get_market_status` | OK | No — free |
| `get_option_chain`, `get_option_briefs`, `get_option_depth`, `get_option_trade_ticks`, `get_option_analysis` | permission denied | Yes — `usOptionQuote` |
| `get_stock_briefs`, `get_trade_ticks` | permission denied | Yes — `usStockQuote` |

`get_quote_permission()` reports exactly one permission held:
`aStockQuoteLv1` (China A-share L1, permanent). Nothing for US markets.

**Contract metadata is a separate entitlement from quote data, and we have
it.** That is what makes the manual architecture work: the machine verifies
contract identity for free, and only bid/ask/volume/OI/limit price are typed.
An impossible strike is refused by Tiger itself with `ERROR 1200 bad_request`.

`option_contract_by_symbol()` and `option_contract()` are **pure local
constructors** — no network, no validation. They will happily build a contract
that does not exist. They are for assembling an order object, never for
verification.

### When `usOptionQuote` is bought

Set `MARKET_DATA_SOURCE=tiger` in `.env` and write `TigerQuoteProvider` in
`service/market/quotes.py`. **No other file should change** — if one does, the seam has
leaked. Until then that setting raises `NotImplementedError` with a message
pointing at the unbought entitlement, rather than silently falling back.

Then restore the chain display from git (see §3c) and re-check it against
live data: row widths, no `nan`, the
ATM marker on the strike nearest spot, thin rows flagged, and the header saying
`real-time` rather than `delayed ~15 min`.

---

## 3. Commission: mostly a flat fee, and it dictates minimum position size

**Settled by measurement on 2026-09-03.** Four real paper orders, two sizes,
both directions:

| Order | Qty | Avg fill | Commission |
|---|---|---|---|
| BUY `44506652990393344` | 1 | 0.2800 | **$3.02** |
| SELL `44506900356154368` | 1 | 0.2800 | **$3.02** |
| BUY `44507006831184896` | 3 | 0.0700 | **$3.09** |
| SELL `44507017730476032` | 3 | 0.0600 | **$3.10** |

### Which model it fits: neither of the simple two

- **Flat per order** would have predicted $3.02 for three contracts. Out by $0.07.
- **Per contract** would have predicted $9.06. Out by $5.97.

It is a **base fee plus a small per-contract component**:

```
commission per order  ~=  $2.985  +  $0.035 x contracts

  1 contract  ->  2.985 + 0.035 = $3.02   (matches, both directions)
  3 contracts ->  2.985 + 0.105 = $3.09   (matches the buy exactly)
```

The 3-contract sell came back at $3.10, a cent above the buy. Too small to
model from one observation — rounding, or a tiny proceeds-based regulatory fee
that applies on sales only. It does not change the shape.

**The base dominates.** Going from one contract to three added seven cents.
Commission is, for practical purposes, a fixed ~$3.00 toll per order.

### What that costs, as a percentage

As actually traded — note this is *not* like-for-like, because the
three-contract trade also used a cheaper contract:

| Trade | Premium | Round trip | % of premium |
|---|---|---|---|
| 1 contract @ 0.28 | $28.00 | $6.04 | **21.6%** |
| 3 contracts @ 0.07 | $21.00 | $6.19 | **29.5%** |

The larger trade looks *worse* only because the contract was cheaper. To see
what size actually does, hold the contract price constant at $0.28/share
($28 per contract) and vary quantity:

| Contracts | Premium | Round-trip commission | % of premium |
|---:|---:|---:|---:|
| 1 | $28 | $6.04 | **21.6%** |
| 2 | $56 | $6.11 | 10.9% |
| 3 | $84 | $6.18 | 7.4% |
| 5 | $140 | $6.32 | 4.5% |
| 10 | $280 | $6.67 | 2.4% |
| 20 | $560 | $7.37 | 1.3% |
| 50 | $1,400 | $9.47 | 0.7% |

### What it implies for a minimum position size

Because the fee is essentially fixed, the percentage is set by **total premium**,
not by contract count as such:

- below **10%** of premium at about **$84** of premium
- below **5%** at about **$140**
- below **2%** at about **$364**

**A $28 position is not a small trade, it is a bad one.** It pays a 21.6%
round-trip toll before the market does anything at all, and it needs a 21.6%
move just to break even. That is not a risk-management problem; it is
arithmetic.

Demonstrated rather than argued: the 1-contract round trip bought at 0.28 and
sold at 0.28 — the price did not move — and `realized_pnl` came back **−$6.04**,
entirely commission. The 3-contract round trip lost **−$9.19**, of which $6.19
was commission and $3.00 was a genuine one-cent-per-share price move.

**Rule of thumb: keep total premium above ~$150 per position** so commission
stays under about 4%, and treat anything under ~$85 as a trade the fee
structure has already decided against.

### Where this leaks into the code

- `positions.py` uses `average_cost`, which Tiger reports **per share and
  including commission**, so Phase 6 profit and loss is already net of the
  entry fee. That is why a $28.00 fill shows a $31.02 cost basis.
- `pricing.py` computes break-even from the limit price and therefore
  **excludes commission**. On a $1,160 order that is noise; on a $28 order it
  is most of the position.
- `orders.estimate_round_trip_commission(quantity)` implements the fitted
  model exactly: `2 x (2.985 + 0.035q)`. It takes **no multiplier**, because
  commission is charged per contract and not per share — an unused shares
  parameter would imply otherwise, which is the misconception this whole
  section exists to correct. The one cent seen on the 3-contract sell is
  unmodelled, so the estimate runs a cent light on the sell side of a
  multi-contract round trip.
---

## 3a. Attached orders (Phase 7) — everything that had to be discovered live

Legs attach to a **parent order** and activate when it fills. They **cannot be
attached to a position you already hold**. To bracket an existing holding you
must close it and buy again with legs attached.

### The broker will not check a bracket before you send it

```
preview_order(bracketed order)
  -> code=1010 biz param error(OCA/ATTACHED order preview not supported)
```

The same refusal for **options and for stocks**, while a plain option order
previews fine (`is_pass=True`). `preview_order` uses the `PREVIEW_ORDER` wire
method, entirely separate from `PLACE_ORDER`, so it is a safe read-only probe —
it simply does not accept attached orders.

**Consequence:** a plain order can be validated before sending; a bracketed one
cannot. The local checks in `orders.validate_bracket_prices` and the preview
block are the only pre-submission check that exists. That is why they are
louder than they would otherwise need to be.

### Confirmed by placing real orders

| Question | Answer |
|---|---|
| Do attached legs work on **options**? | **Yes.** Every doc example uses a stock; options work too. |
| Can **both** PROFIT and LOSS attach to one parent? | **Yes.** Tiger's app help says one sub-order; that does not describe the API. |
| Does **GTC** work on a leg, on a paper account? | **Yes** — see below. |

Two live orders, both filled, all four legs confirmed:

```
44506905057837056   AAPL 260918C00360000  BUY 1 @ 0.30, filled 0.2800
  child 44506905057840128  STP  SELL 1  aux_price   0.15  DAY  HELD
  child 44506905057838080  LMT  SELL 1  limit_price 0.60  DAY  HELD

44506965315701760   AAPL 260918C00370000  BUY 1 @ 0.16, filled 0.1400
  child 44506965315961856  STP  SELL 1  aux_price   0.07  GTC  HELD
  child 44506965315831808  LMT  SELL 1  limit_price 0.40  GTC  HELD
```

### GTC on legs is accepted and genuinely stored

The parent cannot use GTC — Tiger's docs are explicit that paper accounts do
not support it there. **A leg can.** Verified two ways, because a silent
downgrade to DAY would be worse than a rejection: believing you have overnight
protection when you do not is the dangerous failure.

1. The child orders returned by `get_orders` reported `time_in_force='GTC'`.
2. `get_order(id=...)` queried directly on each child **also** returned
   `'GTC'` — the value as stored, not an echo of what was sent.

The control that makes this conclusive: the DAY-leg children from the earlier
bracket still read `'DAY'` on the same query. If the field were echoing input
or defaulting, both sets would read alike. They do not.

`build_option_order_with_bracket(..., leg_time_in_force=...)` is parameterised
and defaults to `DAY`, which is the SDK's own default for `order_leg`. GTC is
now known to work, so the default is a conservatism rather than a limitation.

### Where the legs actually live

**Legs are exposed as CHILD ORDERS carrying `parent_id`, not as entries on the
parent's `order_legs` attribute.** `get_attached_legs` tries both routes; only
the child-order route ever returned anything. Checking `parent.order_legs`
alone would lead you to conclude the legs were never created.

**The two legs use different price fields:**

| Leg | Becomes | Price field |
|---|---|---|
| `LOSS` (stop) | order_type `STP` | **`aux_price`** |
| `PROFIT` (target) | order_type `LMT` | **`limit_price`** |

Reading `limit_price` off a stop leg returns `None`, and vice versa.

### What the SDK puts on the wire

From `tigeropen/trade/request/model.py` `_parse_leg_param`:

- one `PROFIT` leg → `attach_type='PROFIT'`, `profit_taker_price`,
  `profit_taker_tif`, `profit_taker_rth`
- one `LOSS` leg → `attach_type='LOSS'`, `stop_loss_price`, `stop_loss_tif`,
  `stop_loss_rth`, optionally `stop_loss_limit_price` and trailing fields
- **both → `attach_type='BRACKETS'`**, which the documented appendix lists as a
  valid attach type alongside PROFIT and LOSS

`leg_type` values: `PROFIT`, `LOSS`. `LMT`/`STP`/`STP_LMT` also exist but are
**OCA orders only**, a different mechanism.

Signatures, from the SDK because the docs give usage but not definitions:

```python
order_leg(leg_type, price=None, time_in_force='DAY', outside_rth=None,
          limit_price=None, trailing_percent=None, trailing_amount=None,
          quantity=None)

limit_order_with_legs(account, contract, action, quantity, limit_price,
                      order_legs=None, time_in_force='DAY')
```

Only **limit** orders support attached orders.

---

## 3b. The HTTP API (Phase 8)

**A second entry point over the same library, not a rewrite.** Every route
calls the functions the CLI scripts call, and no business logic lives in a
handler. `scripts/` is unchanged in behaviour and still works — those scripts
are the verified evidence behind everything in this file, and they stay.

```bash
python -m api.main                                      # 127.0.0.1:8000
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/health
```

Interactive docs at `/docs`.

### Lock 0: the API key

An HTTP port that can place orders is a different risk from a CLI. Assume
anything that can reach the port will try it.

- Every route except `/health` and the docs requires a matching `X-API-Key`
  header. Missing or wrong is **401 before routing**, compared with
  `secrets.compare_digest` so a wrong key takes the same time to reject as a
  right one.
- **The service refuses to start without `TIGER_API_KEY` set.** `create_app()`
  raises `ConfigError` rather than serving an unauthenticated order endpoint.
  A service that silently came up open would be worse than one that would not
  come up at all.
- It binds **127.0.0.1** by default. The startup banner prints the bind
  address, and prints a warning line if it is not localhost.
- Order endpoints return **403** with the reason when `DRY_RUN` is true or the
  account is not PAPER, and every order-path request is logged with its client
  IP to `logs/api_requests.log` alongside the existing audit record.
- The key is declared as an OpenAPI security scheme, so `/docs` shows an
  **Authorize** button and "Try it out" sends the header. That is presentation
  only: **enforcement lives in the middleware**, which runs whether or not a
  caller ever looks at the schema. `/health` is left unmarked so the schema
  matches `UNPROTECTED_PATHS` exactly, and a test asserts that.

The 403 is a fast, clear refusal **in front of** the real guard, not a
replacement for it. `assert_order_allowed` still runs twice inside `orders.py`
on every order path. Deleting the 403 pre-check would change the error a caller
sees; it would not change whether an order could be placed.

### Orders are two-step

```
POST /orders/preview   ->  full preview + preview_token + expected_cash
POST /orders           ->  that token + that exact expected_cash
```

**The prices are not resent when submitting.** `POST /orders` accepts only a
token and a cash figure. The validated intent — contract, quote snapshot, cost
estimate, bracket prices — is held server-side against the token.

That is the whole point. If a client could restate prices at submit time, it
could preview at one price and submit at another, and `expected_cash` would be
confirming a figure that no longer described the order. Holding the intent
server-side makes the confirmation mean something.

`expected_cash` is the HTTP equivalent of typing the cash amount at the CLI. A
mismatch is refused with `CASH_MISMATCH` rather than reconciled, and the token
is consumed either way.

### Tokens

| Property | Why |
|---|---|
| TTL matches `--max-quote-age` (60s, `PREVIEW_TOKEN_TTL_SECONDS`) | A preview built from a typed quote goes stale for exactly the reason the quote does. One setting, one concept. |
| **Single use** — redeeming deletes it | This is what makes `POST /orders` safe to retry. A client that times out and resends presents a spent token and gets `TOKEN_INVALID`, never a second order. |
| Consumed even when expired | Otherwise a slow retry could succeed after the price had moved. |
| In memory only; a restart invalidates all | A pending confirmation should not outlive the process that made the promise. |
| Guarded by a lock, so redeem is atomic | Two concurrent submits of one token must not both win. A threaded test fires eight at once and asserts exactly one succeeds. |

### The interactive controls, translated

| CLI | HTTP |
|---|---|
| five typed values, re-prompt on a bad one | request body fields; **400** naming which check failed, because there is nobody to re-prompt |
| `USE 115.00` override phrase | `confirm_price_override: true` — a body field, absent by default, **never a query parameter** |
| typed cash confirmation | `preview_token` + exact `expected_cash` |

Checks that are yes/no prompts at the CLI — a wide spread, a limit outside the
quoted market — become **warnings on the preview** rather than blocks, since an
API has nobody to ask. The decimal-slip check keeps its blocking status and
returns the full evidence:

```json
{"error_code": "PRICE_LOOKS_LIKE_DECIMAL_SLIP",
 "detail": {"typed_value": 2.5, "last_close": 0.07,
            "last_close_date": "2026-09-03", "last_close_age_days": 1,
            "ratio": 35.7143,
            "resubmit_with": {"confirm_price_override": true}}}
```

### Errors: coarse status, precise code

Every error body is `{error_code, message, detail}`. **Branch on `error_code`,
never on `message`** — the message is written for a human at 2am and will be
reworded; the code is stable.

| Exception / condition | Status | `error_code` |
|---|---|---|
| `ExpiredContractError` | **410 Gone** | `EXPIRY_EXPIRED` |
| `ExpiryNotListedError` | 404 | `EXPIRY_NOT_LISTED` |
| `StrikeNotFoundError` | 422 | `STRIKE_NOT_FOUND` |
| `BracketError` | 422 | `BRACKET_INVALID` |
| `PricingError` | 422 | `PRICING_FAILED` |
| `ContractError` (base) | 400 | `CONTRACT_INVALID` |
| quote sanity check failed | 400 | `QUOTE_REJECTED` |
| decimal slip, no override | 400 | `PRICE_LOOKS_LIKE_DECIMAL_SLIP` |
| bad or spent token | 400 | `TOKEN_INVALID` |
| expired token | 400 | `TOKEN_EXPIRED` |
| cash figure disagrees | 400 | `CASH_MISMATCH` |
| `LiveTradingBlocked` / locks closed | **403** | `BLOCKED_BY_SAFETY_LOCK` |
| missing or wrong API key | **401** | `UNAUTHORIZED` |
| Tiger entitlement refusal | 502 | `UPSTREAM_PERMISSION_DENIED` |
| broker refused the order | 502 | `UPSTREAM_REJECTED` |
| body failed validation | 422 | `REQUEST_INVALID` |

**410 Gone for an expired expiry** is the mapping worth understanding. It means
precisely "this existed and no longer does", which is the Phase 3 finding
expressed in the protocol: a client can tell "you mistyped a date" (404) from
"that contract has expired" (410) without reading a word of prose.

`UPSTREAM_PERMISSION_DENIED` names an unbought entitlement as a **purchase**
rather than flattening it into a generic upstream failure.

### The deadlock this phase introduced, and how it hid

`api/shared.py` (then called `deps.py`) originally used a plain
`threading.Lock`. `get_quote_client()`
acquires it and then calls `get_settings()`, which acquires the same lock on
the same thread. A plain `Lock` is not reentrant, so that deadlocks.

**It hid behind the two things I tested first.** `GET /health` only calls
`get_settings()` — a single acquisition, no nesting, so it passed. The 401
tests were refused by the middleware *before* reaching a route, so they never
built a client either. The first authenticated request that actually needed a
Tiger client hung. Not crashed — **hung**, with no traceback and no log line,
because uvicorn logs a request on completion and this one never completed.

Now `threading.RLock`, with a comment at the declaration saying why. Two tests
stop it coming back:

- one asserts the lock is an `RLock` by type
- one acquires it nested on a worker thread and asserts completion within a
  five-second timeout, so a regression fails the suite instead of hanging it

The lesson generalises: a health check that touches nothing proves nothing
about the paths that touch something.

### The three dormant functions

Kept, not deleted, and each now carries a `DORMANT, not dead` line in its
docstring. Without it, "unused" and "waiting on an entitlement" look identical
to a future reader.

| Function | Why it is unused | What would activate it |
|---|---|---|
| `market.fetch_contract_quote` | `get_option_briefs` needs `usOptionQuote` | Buying that entitlement. It is the natural body of the `TigerQuoteProvider` the seam is waiting for. |
| `pricing.normalise_limit_price` | Tiger returns `min_tick` as `None`, so there is nothing to snap to | A feed that reports tick sizes |
| `contracts.parse_identifier` | Every current caller starts from the four elements, not the string | Anything that reads identifiers back from Tiger |

All three are spec deliverables. Deleting them would remove things the
specification asked for because an entitlement has not been bought yet.

### New `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `TIGER_API_KEY` | *(none — service will not start)* | Lock 0. A long random value. |
| `API_HOST` | `127.0.0.1` | Bind address. Change only with intent. |
| `API_PORT` | `8000` | |
| `PREVIEW_TOKEN_TTL_SECONDS` | `60` | Matches the quote staleness limit. |

### Verified live, 2026-09-04

Health; 401 without a key and with a wrong key; a full preview → submit cycle
on `AAPL 260918C00380000` (`expected_cash` $9.00, order `44513085863250944`);
the same token retried and refused with `TOKEN_INVALID`; a decimal-slip 400
carrying its evidence, then accepted with `confirm_price_override`; a 403 under
`DRY_RUN=true`; and `GET`/`DELETE` on the real order.

That order **did not fill** — it was 03:31 ET and the market was closed — and
was correctly reported as `NOTHING FILLED` with `settled: false` after 12
polls, not as a success. It was then cancelled through
`DELETE /orders/{order_id}`, which returned `CANCELLED`, `0/1 filled`.

---

## 3c. The simplification (Phase 9)

The project had grown to two top-level packages and 48 files, and the layout
no longer told you where anything lived. Files were named after the **phase**
that built them rather than the **question** they answer, so `orders.py` alone
held 1,548 lines covering arithmetic, printing, submission and cancellation.

Reshaped for readability. **No behaviour changed**: all tests pass and
every remaining script was re-run against the live paper account afterwards.
(236 after the `/capabilities` removal below; 240 before it.)

### The shape now

`tiger_backend/` no longer exists. Everything it held is in **`api/service/`**,
one folder per subject, one file per question:

| Folder | Files | Was |
|---|---|---|
| `service/core/` | `safety`, `config`, `broker`, `audit` | `safety.py`, `config.py`, `clients.py`+`throttle.py`, `audit.py` |
| `service/market/` | `read_data`, `calendar`, `prices`, `quotes` | `market.py`, `providers.py` |
| `service/contract/` | `errors`, `identifiers`, `resolve` | `contracts.py` |
| `service/order/` | `cost`, `build`, `bracket`, `status`, `submit` | `pricing.py`, `orders.py` |
| `service/position/` | `holdings`, `valuation` | `positions.py` |

Each folder's `__init__.py` re-exports its public names, so callers import from
the folder: `from api.service.order import buy_option, estimate_cost`. Moving a
function between files inside a folder therefore breaks nothing outside it.
`core/` is the exception -- you name the file, because `core.safety` reads
better than a bare `core`.

Counts: 53 Python files, up from 48. **File count went up; file size went
down**, which is the trade that was wanted. Nothing in `service/` is over 700
lines, and the largest is the manual-entry seam, which is one coherent thing.

### The one that matters: `order/submit.py`

`orders.py` was 1,548 lines with the submission path buried in the middle. It
is now five files, and the whole spend path is one of them, at 427 lines:

```bash
grep -rn 'trade_client\.place_order(' --include='*.py' api scripts
#   2 hits, both in api/service/order/submit.py
grep -rl '^[[:space:]]*assert_order_allowed(' --include='*.py' api scripts
#   api/service/order/submit.py, and nothing else
```

`cancel_order` was deliberately moved *out* of it into `status.py`, because
cancelling cannot open a position and its presence weakened the claim the file
docstring makes. What is left in `submit.py` is the submission path, and
nothing else.

### What was deleted, and how to get it back

Everything below is in git history. `git log --oneline` and
`git show <sha>:<path>` will bring any of it back.

**Three scripts.** `02_show_chain.py` (blocked on `usOptionQuote` regardless),
`04_simulate_order.py` (superseded by `POST /orders/preview`), and
`07_premium_history.py` (a learning tool).

**The whole chain-table feature**, which existed only to serve `02`. Removing
that script orphaned it: `fetch_option_chain`, `build_option_rows`, `OptionRow`,
`StrikeRow`, `pair_calls_and_puts_by_strike`, `find_atm_strike`,
`select_strikes_around_price`, plus `tests/chain_fixture.py` and
`tests/test_chain_table.py`. Roughly 240 lines and 28 tests.

**Worth knowing before you buy `usOptionQuote`:** the chain display goes with
it. When the entitlement arrives, recover it from git rather than rewriting --
including `chain_fixture.py`, the deliberately lopsided fixture that caught a
real column-width bug. It was uniform at first, and the uniform version hid an
8-wide volume column that fitted `71,626` but not `1,204,553`, so busy rows ran
the volume into the spread column.

**Four dead symbols**, zero uses between them: `reset_for_testing`,
`PreviewTokenStore.outstanding_count`, `PositionError`, and
`get_market_data_provider` (whose only caller left with `04_simulate_order.py`).

**The `/capabilities` endpoint pair.** `GET /capabilities` returned a cached
probe and `POST /capabilities/probe` refreshed it -- twenty live API calls,
about ten seconds. Nothing consumed either one over HTTP, and
`scripts/00_check_capabilities.py` answers the same question in the place the
question is actually asked: at a terminal, once, after buying a market-data
package. The pair cost 250 of the 361 lines in `routes/account.py`, which is
now 69 lines and one endpoint. The API is down to **9 paths**; the four tests
covering the probe's age formatting went with it, so the suite is **236**.

### Three circular imports the restructure exposed

Splitting a file splits its import graph, and the graph has opinions.

**1. `main.py` cannot hold `errors.py`.** Merging them broke the build at once:

```
main.py -> shared.py -> order_rules.py -> main.py   (ApiError)
```

`ApiError` is raised by the lowest-level checks and handled by the highest-level
server, so it must sit **below both**. The same applied to `log_order_request`,
which the routes need while `main.py` imports the routes. So `main.py` is the
root of the import graph -- it imports everything, nothing imports it --
`errors.py` stayed a separate leaf module, and the request logger moved into
`shared.py`.

**2. `market/calendar.py` and `market/prices.py` needed each other.** Both used
the three pandas-cell readers and the shared `MarketDataError`. Those moved
down into `market/read_data.py`, which neither imports back.

**3. `contract/identifiers.py` and `contract/resolve.py` needed each other,**
over `ContractError`. The four exception types moved down into
`contract/errors.py`.

The pattern in all three: when two files need each other, the thing they share
belongs in a third file *below* both. That is why `read_data.py` and `errors.py`
exist, and both say so in their docstrings.

### Two tests had to change, and why

`monkeypatch.setattr(module, "list_expirations", ...)` has to patch the module
that *does the lookup*, not the package that re-exports the name. After the
split those are different objects: `resolve.py` holds its own reference to the
imported function, so patching `api.service.contract` does nothing. The same
applies to `fetch_last_traded_close`, which `quotes.py` imports from `prices.py`
inside the function body -- that patch has to land on `prices.py`.

Both are commented in place now. It is the one thing about this layout that
will catch you out.

### `ARCHITECTURE.md`

New, and the actual fix for "I cannot find anything". One page:

- the one-brain-two-doors diagram
- the whole `service/` tree, one line per file
- a **"I want to change X, open file Y"** table
- a request traced end to end, through both `assert_order_allowed` calls
- the four locks and their defaults
- the three greps that prove the structural guarantees still hold

Every line count and grep result in it was verified against the tree.

---

## 3d. The tick size, MEASURED (Phase 10)

### The problem

Phase 10 needed to add one tick to a price. Tiger will not say what a tick is:
`read_min_tick` returns `None` from **both** `get_contract` and
`get_derivative_contracts`, which is why the limit price has been a typed
human input since Phase 4.

The plan for Phase 10 was going to hard-code the widely quoted US convention:
**$0.01 below $3.00, $0.05 at $3.00 and above.**

**That convention is wrong for the symbols this account trades.** It was
measured before it was used, and the measurement refuted it.

### The method: read prices that real trades happened at

`get_option_bars` is free -- no `usOptionQuote` needed. Every `open`, `high`,
`low` and `close` in a bar is a price something genuinely traded at, so it sits
on a legal increment by definition. Enough of them, and the grid is measured
rather than assumed.

Run on **2026-09-05**, market closed, entirely read-only:

| Symbol | Traded prices | Sub-cent | Off-nickel | Off-nickel at ≥ $3.00 | Grid |
|---|---|---|---|---|---|
| AAPL | 2,516 | 0 | 1,381 | 486 | **PENNY** |
| SPY | 860 | 0 | 722 | 332 | **PENNY** |
| TSLA | 1,796 | 0 | 1,058 | 223 | **PENNY** |
| MSFT | 2,068 | 0 | 1,166 | 468 | **PENNY** |
| NVDA | 2,180 | 0 | 1,472 | 353 | **PENNY** |
| BRK.B | 22,940 | 0 | 8,940 | 4,396 | **PENNY** |
| **total** | **32,360** | **0** | **14,739** | **6,258** | |

A deeper AAPL-only run over 4,944 prices found the same thing in every band:

```
BAND                  PRICES  SUB-CENT   VERDICT
under $1.00             1932         0   PENNY ($0.01)
$1.00 to $2.99           514         0   PENNY ($0.01)
$3.00 to $9.99           707         0   PENNY ($0.01)
$10.00 and above        1791         0   PENNY ($0.01)
```

Real traded prices above $3.00 that are **not** on a nickel: `114.12`,
`116.64`, `149.06`, and 6,255 others.

### The measured answer

> **$0.01 at every price level, on all six symbols tested.
> Zero sub-cent prices in 32,360 observations.**

There is no $3.00 boundary for these names. They are penny-quoted throughout.

### Why this mattered so much

Had the band rule been implemented as planned, every entry buffer above $3.00
would have been **five times larger than intended**, silently:

| Entry | Planned (band rule) | Measured (correct) | Error |
|---|---|---|---|
| $0.30 | 0.31 | 0.31 | none |
| $8.05 | **8.10** | **8.06** | **+4c per share, +$4 per contract** |

Nobody would have noticed. The order would have filled, at a worse price, and
the code would have looked right.

### What is NOT settled, and why it does not block

The US market was closed (Saturday; next open Monday 2026-09-08 09:30 ET), so
two of the four planned questions could not be answered:

1. **Does Tiger reject a sub-tick price, or silently round it?**
2. **Are the attached legs tick-validated the same way?**

Neither blocks Phase 10, because **every price this code produces is already on
the grid** -- so the rejecting-versus-rounding behaviour is never reached on
the happy path. It matters for the error message on a bug, not for correctness.

**To settle it, during US market hours:** place a resting BUY limit far *below*
the market so it cannot fill -- e.g. `$8.07` on a contract asking $20 -- then
read the stored `limit_price` back with `get_order` and cancel. If the stored
price differs from the sent price, Tiger rounds silently, and that is worth
knowing. Repeat with a legal entry and an illegal take-profit for question 2.

### If a symbol turns out to quote more coarsely

Penny-interval membership is per option class, and only six symbols were
tested. A nickel-quoted class would reject a penny price -- a **clean refusal**,
`502 UPSTREAM_REJECTED` carrying the broker's own message, not a bad fill.

Set `OPTION_TICK_SIZE=0.05` in `.env` for that case. Re-measure first, with the
same method: pull `get_option_bars` for a spread of that symbol's contracts and
check whether any traded price is off-nickel.

---

## 3e. The fast single-call trading API (Phase 10)

`POST /trade` takes seven trading inputs and places one bracketed BUY.

```
{ symbol, option_type, current_price, quantity,
  entry_price, take_profit_percent, stop_loss_percent,
  client_order_id, max_cash }
        |
        v  resolve contract (cached)   ->  0 calls warm, 4 cold
        v  snap + buffer entry         ->  local
        v  TP/SL from percentages      ->  local
        v  ONE place_order with legs   ->  1 call
        v  one status read             ->  1 call
BUY + TAKE PROFIT + STOP LOSS, in a single Tiger call
```

### Prices

Percentages apply to the **buffered** entry, not the price that arrived,
because the buffered price is what will actually be paid.

```
entry_price 0.30  -> snap 0.30 -> +1 tick -> 0.31   THE BUY LIMIT
  TP  0.31 x 1.20 = 0.3720  -> ceil  -> 0.38
  SL  0.31 x 0.85 = 0.2635  -> floor -> 0.26
```

**Rounding is deliberately asymmetric: TP up, SL down. The bracket only ever
widens.** Rounding a target down would sell for less than was asked; rounding a
stop up would trigger it sooner, and worse, than was asked. Neither leg can
fire earlier than intended because of a rounding artefact.

A stop that floors below one tick is refused (`BRACKET_INVALID`) rather than
sent as zero -- this bites on very cheap contracts.

### There is no cash ceiling, by request

`max_cash` was built as the single-call replacement for the two-step
`expected_cash` echo: a limit the client stated up front, refusing the order
with 422 before anything was sent. **It was removed on 2026-09-07 at the
owner's instruction.**

What that gives up, recorded so it is not rediscovered as a surprise:

- **A decimal slip in `entry_price` is no longer caught.** `30` instead of
  `0.30` is a $3,000 order rather than a $30 one, and nothing refuses it. The
  two-step endpoint still catches this via the last-traded-close comparison;
  this one skips that call for speed.
- **A surprising strike is no longer caught.** This endpoint *chooses* the
  strike, and at one spot price the choices differ by 27x:

```
current_price 318.40  ->  AAPL 320 CALL ~ $8.05
current_price 318.40  ->  AAPL 360 CALL ~ $0.30
```

`validate_only: true` remains the way to see the cost before committing, and
`cash_required` is still in every response. Restoring the ceiling means adding
one optional field and one comparison -- see commit history for the original.

### Idempotency

`client_order_id` is claimed **before** the order can reach the broker, not
after. A retry arriving mid-flight gets `409 REQUEST_IN_FLIGHT`; a retry after
completion replays the original response with `duplicate: true`.

A crash between claim and completion leaves the key stuck. That is deliberate:
refusing a retry costs a missed trade, allowing one costs a duplicate position.
Recovery is `GET /orders/{id}`.

**In memory, so it dies with the process** -- correct for one instance, wrong
behind a load balancer. Note it before scaling out.

### What the fast path skips, and what it does not

| Skipped | Why |
|---|---|
| option quote fetch | caller supplies `entry_price`; entitlement not owned anyway |
| underlying price fetch | caller supplies `current_price` |
| cash-available check | advisory, and a round trip |
| decimal-slip check | a round trip; `max_cash` covers it locally |
| settle polling | `poll_attempts=1` -- one read, no sleeping |
| leg confirmation | `legs_confirmed` is always `false`; use `GET /orders/{id}/legs` |

**Nothing in the safety system was touched.** `core/safety.py`,
`order/submit.py`, `order/status.py`, `core/audit.py` and
`market/quotes.py` are all unmodified. `/trade` calls the same
`buy_option_with_bracket` the CLI does, with both `assert_order_allowed` gates
and the single `place_order` exactly where they were.

`volume` and `open_interest` are left `None` rather than invented.
`is_low_liquidity` treats missing data as thin, so the gap fails safe.

### The fourth circular import

`config.py` needed the tick and expiry defaults, which live in
`order/ticks.py` and `contract/selection.py`. Importing them gives:

```
core/config -> contract/selection -> contract/__init__ -> resolve
            -> core/broker -> core/config
```

**`core/` is the foundation and may not import a subject package.** The two
constants are therefore *copied* into `config.py`, and `tests/test_ticks.py`
asserts the copies still agree -- the cheap half of what the import would have
bought. Same lesson as §3c: the import graph has opinions.

---

## 3f. Making the trade endpoint readable (Phase 10a)

`routes/trade.py` had one function of **185 lines**. Every other route file in
the project is smaller than that in total. The flow was invisible.

### The route is now four steps

```python
replay = claim_request_id(...)      # 1. seen this request before?
if replay is not None: return replay

try:
    plan = prepare_trade(body)      # 2. work it out. CANNOT SEND.
except Exception:
    release_request_id(...); raise

if body.validate_only:
    return ... describe_only(plan, body)      # 3. just testing?

return ... submit_and_record(plan, body, request)   # 4. send it
```

A `TradePlan` dataclass carries the decisions between steps, which retired the
`common = dict(...)` that used to be unpacked twice with `**`.

| | Before | After |
|---|---|---|
| Longest function | **185** | **66** |
| The route itself | 185 | **35** |

The file grew from 428 to 493 lines, because nine small documented functions
cost more lines than one big undocumented block. That was the trade wanted.

### The `placed` flag is gone, and that is a safety improvement

There used to be a `placed = False` variable flipped to `True` just before
submission, with the error handler consulting it to decide whether releasing
the idempotency key was safe. Trusting it meant tracing the flag.

`prepare_trade` now **imports nothing that can place an order**, so a failure
inside it provably reached no broker. The release sits in one place, wrapping
only that call. Structure instead of a flag, and two tests assert it.

### Moved to where their siblings already live

| What | To | Why |
|---|---|---|
| Choosing + verifying a contract | `service/contract/selection.py` | It is business logic; routes do not hold that |
| `shape_bracket_prices`, `shape_tick`, `shape_submitted_legs` | `api/schemas.py` | Every other `shape_*` is there |
| `build_price_only_snapshot` → `build_price_only_quote` | `api/order_rules.py` | Sits beside `build_quote_snapshot`, its sibling |
| `TICK_SOURCE` note | `service/order/ticks.py` | Belongs with the measurement it describes |

`SymbolNotListedError` was added so a bad symbol returns **404 SYMBOL_NOT_FOUND**
from the exception map, rather than the route hand-rolling the error.

### Three files renamed for plain English

| Was | Now | Why |
|---|---|---|
| `api/wiring.py` | `api/shared.py` | "wiring" is a metaphor; these are shared things built once |
| `service/order/lifecycle.py` | `service/order/status.py` | It answers "what happened to my order?" |
| `service/market/fields.py` | `service/market/read_data.py` | "fields" said nothing |

`api/app.py` also went back to `api/main.py`, because debugger launch configs
point at it and the rename in Phase 9 broke them for no gain.

### A hand-testing form

`GET /ui` serves a plain HTML form for `POST /trade`, from
`routes/trade_form.html`. It is served **by the API itself** rather than opened
as a file, because a `file://` page calling `127.0.0.1` is cross-origin and the
browser blocks it — serving it same-origin avoids opening CORS on a service
that can place orders. `/ui` needs no key (it is static HTML holding no
secrets); the order it sends still does.

329 tests pass. Behaviour unchanged, which is what the unchanged tests prove.

---

## 3g. Two bugs a live order found, 2026-09-08

Both were found by placing real orders on the paper account with the market
open. Neither showed up in 327 offline tests.

### 1. A filled order returned 500

`submit_and_record` read `outcome.filled`. The field is `filled_quantity`.

Order 44561393351150592 was placed, reached Tiger, and FILLED. The caller got
a 500 and never learned the order id. **The money moved and the reply was
lost** -- the worst shape this class of bug takes, because the idempotency key
is then stuck IN_FLIGHT and a retry is refused (correctly), while the caller
has no id to go and look up.

Why nothing caught it: the `/trade` tests read the route's SOURCE TEXT for
structure and never once built a response from a real `FillOutcome`. The
two-step endpoint was unaffected -- it goes through `shape_fill()`.

Two tests added, both failing against the old code. One builds a real
`FillOutcome` and a real `TradePlan` and calls `build_response`. The other
regexes every `outcome.X` out of `submit_and_record` and asserts `FillOutcome`
has `X` -- that one catches the whole class without placing an order.

### 2. JavaScript rounds Tiger's order ids

```
real order id      44561462560050176
browser asked for  44561462560050180     -> 500, then 404
```

**Tiger order ids exceed 2^53**, the largest integer JavaScript holds exactly.
`44561462560050176` is 4.9x past it. The number survives `JSON.parse`, but
`String(id)` prints the shortest form that round-trips, which is
`44561462560050180` -- so the id in the URL is not the id the broker issued.

**Any JavaScript frontend hits this.** The fix in `trade_form.html` is to read
the response as TEXT and pull the digits out with a regex before anything
parses them:

```js
const raw = await r.text();
const exactOrderId = raw.match(/"order_id"\s*:\s*(\d+)/)[1];   // a string
const data = JSON.parse(raw);
```

Python is not affected: its integers are arbitrary precision.

Still open: whether to return `order_id` as a JSON string as well, so a naive
client cannot get this wrong. That changes the response shape, so it is a
decision, not a fix.

### 3. And a missing order returned 500

Tiger reports an unknown id as a plain `ApiException` carrying
`not_found:Order does not exist`. Nothing in `EXCEPTION_MAP` matched the type,
so it fell through to `INTERNAL_ERROR`. It is now **404 ORDER_NOT_FOUND**,
matched on the message, and the message names the JavaScript trap because
that is the likeliest cause.

333 tests pass.

---

## 3h. Strike selection for fast trading (2026-09-09)

Measured on the live TSLA 11-Sep chain, spot 371.79, volume over ten minutes:

| | median volume | median 1-min move |
|---|---|---|
| WHOLE strikes (no .5) | **1,067** | |
| HALF strikes (x.5) | 309 | |
| IN the money | 1,472 | 5.0% |
| OUT of the money | 3,634 | **9.7%** |

Whole strikes carried **3.5x** the volume of the half strikes beside them --
375.0 had 3,538 against 377.5's 774, and it held at every level. Out-of-the-
money contracts carried 2.5x the volume of in-the-money ones and moved nearly
double the percentage per minute. The deep ITM strikes were dead: 350 and 355
showed 0.0% median movement.

More volume means more trades, which means the free one-minute bar updates
more often. That is the whole reason this matters.

`find_otm_whole_strike()` therefore filters: whole numbers, then out of the
money, then the Nth one out. A strike sitting exactly ON the spot is at the
money, not out of it, and is skipped.

**Whole is `strike == int(strike)`, not `strike % 5 == 0`.** Ladder spacing
follows the price of the underlying -- 2.5 on TSLA, 1.0 on a $30 stock -- so
a hardcoded 5 would filter out every strike on a cheap name and leave nothing
to choose from.

`find_closest_strike` is untouched and still used for error messages.

### age_seconds is not freshness, and cannot be made into it

A request came in to refuse any price older than 3 seconds. **One-minute bars
cannot support that**, and the reason is worth writing down.

`age_seconds` measures time since the bar's MINUTE BEGAN, not since the last
trade. Measured live on a liquid contract:

```
wall time   bar minute   AGE    price
10:06:42      10:06      43s    5.27
10:06:49      10:06      49s    5.00   MOVED
10:06:55      10:06      56s    4.95   MOVED
10:07:00      10:06      60s    5.00   MOVED
```

The age climbed to 60s while the price updated every two seconds and the
bar's volume went 518 -> 637. A 3-second limit would have rejected all of it.

The question the data CAN answer is whether the contract traded during the
current minute. `RecentTrade.is_live` tests two things: the newest bar is the
current minute, AND its volume is above zero. A current-minute bar with zero
volume is a placeholder carrying an older trade forward -- exactly the thin-
contract case where the price sat unchanged for 33 seconds.

`require_live_trading` (default false, on in the form) enforces it and returns
**422 PRICE_NOT_LIVE**. Every response now reports `is_live` and
`recent_volume` whether or not it was enforced, so the freshness is visible
even when it is not being checked.

### What free data supports, measured

| Contract | Update rate | Move |
|---|---|---|
| 9-day, thin | unchanged for 33s | 0.6% over 40s |
| 2-day, 499/5min | changed 15 times in 34s | **7.8% over 34s** |

Scalping at the seconds-to-minutes scale works on liquid contracts and not on
thin ones. The data was never the limit; contract choice was.

---

## 4. Environment facts

Not derivable from the repo, because `.env` and `secrets/` are gitignored.

| Fact | Value |
|---|---|
| Licence | **TBSG** (Singapore). `TIGER_LICENSE=TBSG` in `.env`. TBSG does **not** need the `tiger_openapi_token.properties` file TBHK requires. |
| Paper account | 17 digits, ends `6574`. In both `TIGER_ACCOUNT` and `TIGER_PAPER_ACCOUNT`, identical — that is Lock 1. |
| Private key | `secrets/tiger_private_key.pem`, PKCS#8, **headerless**. |
| Market data source | `MARKET_DATA_SOURCE=manual` |
| API key | `TIGER_API_KEY` is set in `.env`. Without it the HTTP service refuses to start. Not in git. |
| API bind | `API_HOST=127.0.0.1`, `API_PORT=8000` |
| Preview token TTL | `PREVIEW_TOKEN_TTL_SECONDS=60`, matching the quote staleness limit |
| Locks | `TIGER_ALLOW_LIVE=false`, `DRY_RUN=true` |
| Virtualenv | `vnv/`, not `.venv`. `vnv\Scripts\activate`. |
| Python | 3.11.9, `tigeropen` 3.7.1 |
| Git | Repo root is this directory. `C:\Users\manoj` is *itself* a git repo (a Cursor worktree accident); never `git add -A` from there. |
| Open positions | 1x `AAPL  260918C00360000` (DAY legs live) and 1x `AAPL  260918C00370000` (GTC legs live). The 3-contract 380 call was opened and closed to settle §3. |

### The private key, and the trap in it

Tiger supplied credentials as `secrets/DEMO_tiger_openapi_config.properties`,
holding the key inline on its `private_key_pk8=` line. It was extracted into
`secrets/tiger_private_key.pem`.

**The .pem contains raw base64 and nothing else — no header lines.**
`read_private_key()` strips only `-----BEGIN RSA PRIVATE KEY-----` markers, so
PKCS#8 headers (`-----BEGIN PRIVATE KEY-----`) would survive and break signing.

Pointing `TIGER_PRIVATE_KEY_PATH` at the `.properties` file costs an hour. The
SDK reads the whole file verbatim and tries to sign with all 1759 characters:

```
Unable to load PEM file ... InvalidData(Invalid symbol 95, offset 7.)
```

Symbol 95 is `_`, at index 7 — the underscore in `private_key_pk8`. `config.py`
now rejects that with a readable message.

### Market data device access — this project overrides the SDK default

`QuoteClient(..., is_grab_permission=True)` claims market-data device access on
construction. Not a purchase, but it **moves** primary-device status to this
machine and takes it from whatever held it — the Tiger app on a phone, for
instance.

`build_quote_client` defaults to `grab_permission=False`. Nothing here needs it.
The consequence:

| Message | Meaning | Fix |
|---|---|---|
| `current device does not have permission` | Another device holds primary status | Re-run with `--grab` |
| `...permissions in the US OPT quote market` | The entitlement was never bought | Buy `usOptionQuote` |

`--grab` is available on `00_check_capabilities.py`,
`03_find_contract.py`, `05_paper_order.py` and
`06_positions.py`.

### Do not switch to props_path

Lock 1 compares the account in `.env` against the declared paper account. If
the SDK sourced its account from a properties file instead, the two could drift
and the check would be guarding a value the SDK no longer uses. `.env` stays
the single source of truth.

---

## 5. Confirmed SDK signatures

Read off the docs and used as written. Do not guess; if something is needed
that is not listed here, fetch the page. Every documented limit is enforced by
`api/service/core/broker.py`, one `RateLimiter` per endpoint.

### Account and orders

```python
TradeClient.get_managed_accounts(account=None, lang=None)                    # 60/min
TradeClient.get_prime_assets(account=None, base_currency=None,
                             consolidated=True, lang=None)                   # 60/min
TradeClient.get_positions(account=None, sec_type=SecurityType.STK,
                          currency=Currency.ALL, market=Market.ALL,
                          symbol=None, sub_accounts=None, expiry=None,
                          strike=None, put_call=None,
                          asset_quote_type=None, lang=None)                  # 60/min
TradeClient.get_orders(account=None, sec_type=None, market=Market.ALL, ...)  # 120/min
TradeClient.get_open_orders(account=None, sec_type=None, ...)                # 120/min
TradeClient.get_order(account=None, id=None, order_id=None,
                      is_brief=False, show_charges=None, lang=None)          # 120/min
TradeClient.place_order(order, lang=None) -> int                             # 120/min
TradeClient.cancel_order(account=None, id=None, order_id=None) -> int        # 120/min
```

### Contracts — https://docs-en.itigerup.com/docs/get-contract

```python
TradeClient.get_contract(symbol, sec_type=SecurityType.STK, currency=None,
                         exchange=None, expiry=None, strike=None,
                         put_call=None, lang=None)                           # 60/min
TradeClient.get_derivative_contracts(symbol, sec_type, expiry, lang=None)    # 60/min
```

### Order construction — local, no network

```python
limit_order(account, contract, action, quantity, limit_price, time_in_force='DAY')
market_order(account, contract, action, quantity, time_in_force='DAY')
option_contract(identifier, multiplier=100, currency='USD')
get_option_identifier(underlying_symbol, expiry, put_call, strike)
extract_option_info(identifier)
```

`time_in_force` and `outside_rth` are set as attributes after construction.
Paper accounts reject `GTC`.

### Market data

```python
QuoteClient.get_option_expirations(symbols, market=None)                     # 60/min
QuoteClient.get_option_chain(symbol, expiry, option_filter=None,
                             return_greek_value=None, market=None, ...)      # 60/min
QuoteClient.get_option_briefs(identifiers, market=None, timezone=None)       # 120/min
QuoteClient.get_option_bars(identifiers, begin_time=-1, end_time=...,
                            period=BarPeriod.DAY, limit=None, ...)           # 60/min
QuoteClient.get_stock_briefs(symbols, include_hour_trading=False, lang=None) # 120/min
QuoteClient.get_stock_delay_briefs(symbols, lang=None)                       # 10/min
QuoteClient.get_quote_permission()                                           # 10/min
```

The three option calls take `market` and this project passes `Market.US`
explicitly on all of them. **The two stock quote calls take no `market`
parameter.**

`get_stock_briefs` reports the price in `latest_price`;
`get_stock_delay_briefs` has no such column and reports `close`.

**`grab_quote_permission()` mutates state and is never called.**

---

## 6. Documentation gotchas

The ones that produce plausible wrong answers rather than errors.

**1. Two different volatilities, confusingly named.** The chain returns
`implied_vol` — what the market expects ahead. `get_option_briefs` returns
`volatility` — *historical* volatility, how much the underlying has already
moved. They live on separate dataclasses (`OptionRow.implied_volatility` and
`ContractQuote.historical_volatility`) so they cannot be confused. Do not merge
them.

**2. Rate limits are not uniform.** 60, 120 and 10 per minute all appear. A
single global throttle would be both wrong and needlessly slow.

**3. The same field has two names.** The chain calls the last trade time
`last_timestamp`; `get_option_briefs` calls it `latest_time`. No error if you
read the wrong one — you get `None`.

**4. `strike` arrives as a STRING in more than one place.** On
`get_derivative_contracts` entries and on a position's `contract`.
`get_contract` returns a float. String sorting puts `'100.0'` before `'95.0'`,
so an unconverted strike silently produces the wrong nearest-strike list.
Convert on the way in, every time.

**5. `identifier` and `name` swap meaning between calls.** `get_contract` gives
`identifier='AAPL  260918C00320000'` and `name='Apple'`.
`get_derivative_contracts` gives `identifier=None` with the OCC code in `name`.
Reading `.identifier` off a ladder entry yields `None`, which then travels on
as a missing contract.

**6. An unlisted expiry returns an empty list, not an error.** Reads as "no
strikes exist" when it means "that expiry does not exist". Different messages.

**7. `min_tick` is always `None`.** No API source for the valid price
increment, which is why the limit price is typed. Do not guess a convention.

**8. `OrderStatus` values are not their names.** `OrderStatus.REJECTED` is the
string `'Inactive'`; `NEW` is `'Initial'`; `HELD` is `'Submitted'`. Comparing
against the wrong one silently never matches. Everything goes through
`orders.normalise_status()`.

**9. `get_positions` defaults to `sec_type=STK`.** Call it without passing
`SecurityType.OPT` and it returns shares and no options at all, with no error.

**10. `average_cost` is per share and includes commission.** See §3. This is
not documented either way; it was established by comparing a known fill against
what came back.

**11. Tiger's `market_price` is `latestPrice`, and `unrealized_pnl` derives
from it.** Both are recorded on `OptionPosition` for comparison but neither
values anything: the spec requires the bid. On the held position Tiger says
−2.02 and the bid says −4.02.

**12. `position_qty` is the real quantity.** `quantity` is documented as the
"legacy scaled" value with `position_scale` as its decimal scale. Prefer
`position_qty`, fall back to `quantity`.

**13. `get_option_bars` ignores `limit`** — asking for 5 returned all 61 — and
returns an empty **list**, not an empty DataFrame, when a contract has no
history. It does work on expired contracts, which is what makes
`07_premium_history.py` possible. That script was removed in Phase 9; the
endpoint is still free, and the script is recoverable from git.

**14. Expiry timestamps are midnight US/Eastern**, not UTC. Reading one in
another zone lands on the wrong calendar day. All conversion goes through
`market.milliseconds_to_date()`.

**15. Option-chain Greeks are deprecated.** Daily updates, unsuitable for
intraday. Never requested, never displayed, no logic built on them.

**16. `preview_order` refuses attached orders.** `code=1010 OCA/ATTACHED
order preview not supported`, for options and stocks alike. A bracket cannot be
validated before it is sent. See §3a.

**17. Attached legs are child orders, not `parent.order_legs`.** Query by
`parent_id`; the parent's own attribute stayed empty in every observation.

**18. A stop leg carries its price in `aux_price`, a target leg in
`limit_price`.** Reading the wrong one returns `None`.

**19. GTC works on a leg even though the parent rejects it** on a paper
account. Confirmed as stored, not just as accepted, with a DAY control. §3a.

**20. Tiger returns already-expired dates in the expirations list.**
2026-09-02 was still the first entry on 2026-09-03. `contracts.resolve_expiry`
therefore has three outcomes, not two — see §7.

---

## 7. The locks, and how they were exercised

| Lock | Mechanism | State |
|---|---|---|
| 0 — API key *(HTTP only)* | `X-API-Key` must match `TIGER_API_KEY`; the service will not start without one | 401 before routing |
| 1 — Account allowlist | configured account must equal `TIGER_PAPER_ACCOUNT` | fails closed |
| 2 — Live opt-in | `TIGER_ALLOW_LIVE` must be `true` for any other account | `false` |
| 3 — Dry run | `DRY_RUN` must be `false` for an order to be sent | `true` |

`assert_order_allowed` is called **twice** in the order path: once as the gate
before the human is asked anything, and again immediately before `place_order`,
so nothing between the gate and the wire can have changed the mode.

All four verified live. Lock 0 refuses a request with no key and one with a
wrong key; Lock 1 refuses a non-paper account ID; Lock 3 blocked the real
order flow before the confirmation prompt when `DRY_RUN=true`, at the CLI and
with a 403 over HTTP.

### What a reviewer should grep for

Three greps check that the structural guarantees still hold. Each currently
passes, and each failure means something specific has gone wrong.

Count the **call sites**, not the mentions. A bare `grep -c assert_order_allowed`
returns 8, because the import and the numbered sequence in two docstrings match
as well -- which is exactly how the first draft of this section came to quote
the wrong number. The anchored patterns below count only what executes.

```bash
# 1. The submission call lives in one file and is reached from exactly two
#    places: the plain order path and the bracketed one. A call site
#    anywhere else is a second submission route outside the guards.
grep -rn 'trade_client\.place_order(' --include='*.py' api scripts
#    expect 2 hits, both in api/service/order/submit.py

# 2. assert_order_allowed is CALLED four times: twice on the plain path and
#    twice on the bracketed one. Once as the gate before the human is asked
#    anything, once immediately before the wire. Three means a guard was
#    dropped from one of the two paths.
grep -c '^[[:space:]]*assert_order_allowed(' api/service/order/submit.py
#    expect 4
grep -rl '^[[:space:]]*assert_order_allowed(' --include='*.py' api scripts
#    expect api/service/order/submit.py, and nothing else

# 3. The provider seam. A concrete provider named outside quotes.py means the
#    abstraction has leaked, and swapping to fetched data will no longer be
#    one line in .env.
grep -rn 'ManualEntryProvider\|TigerQuoteProvider' --include='*.py' api scripts \
  | grep -v 'market/quotes.py'
#    expect nothing at all
```

Grep 3 covers `api` and `scripts` but not `tests`: `tests/test_providers.py` is
the test *of* the seam, so it names the class deliberately and says so in a
comment above the import.
---

## 8. Where things are

**`ARCHITECTURE.md` is the map. This is the inventory.**

```
api/
  main.py         The server. API-key middleware, error handlers, uvicorn entry.
  errors.py      Exception -> (status, error_code), in one table. A LEAF module:
                 folding it into main.py makes main -> shared -> order_rules -> main.
  shared.py      Settings and clients, built once. RLock, not Lock -- see 3b.
                 Also the request log, for the same import-graph reason.
  schemas.py     Every Pydantic shape, and the functions that build them.
  order_rules.py Quote checks, preview tokens, and the idempotency store.
  routes/        health, account, market, contracts, positions, orders,
                 trade (POST /trade), ui (the hand-testing form).
                 Thin: check the request, call the service, shape the reply.

  service/       ALL the logic. One folder per subject, one file per question.

    core/        Foundations. Nothing here knows what an option is.
      safety.py    Three locks, account masking, startup banner. Pure logic.
      config.py    .env -> frozen Settings. Fails closed.
      broker.py    QuoteClient and TradeClient, built separately on purpose,
                   plus the RateLimiter and one instance per endpoint.
      audit.py     JSONL order trail in logs/ (gitignored).

    market/      What exists out there, and what it is worth.
      read_data.py    Reading Tiger's dataframes without crashing, and the one
                   error this folder raises. Below calendar.py and prices.py
                   because both need it.
      calendar.py  Expiries, and every date conversion. US/Eastern, always.
      prices.py    Underlying price, last traded close, spread, liquidity.
                   All pandas access is in this folder and nowhere else.
      quotes.py    THE SEAM. The only file naming a concrete provider.

    contract/    Which exact contract are we talking about?
      selection.py    Which contract, when the caller named none? Pure.
      errors.py       The four failures. Expired is not the same as missing.
      identifiers.py  The two expiry formats and the 21-char OCC identifier.
      resolve.py      Identity only. No market data, so no entitlement needed.

    order/       Everything about an order.
      ticks.py       The price grid. MEASURED, not assumed -- see 3d.
      cost.py        Pure arithmetic, no network. Cash, break-even, max loss.
      build.py       Build the order object and print the preview. Sends nothing.
      bracket.py     Take-profit and stop-loss legs, and the commission model.
      status.py   Status, fills, polling, cancelling. Cannot open a position.
      submit.py      THE ONLY FILE THAT CAN SPEND MONEY. Both place_order calls,
                     all four assert_order_allowed gates, and nothing else.

    position/    What is held, and how it is doing.
      holdings.py    What the account holds, and the cash available.
      valuation.py   P&L at the bid, and the expiry warning.

scripts/         Five. Each one is the evidence behind a finding above.
  00_check_capabilities.py  Diagnostic, outside the phases. Read-only probe of
                            every endpoint. Run after buying market data to see
                            exactly what changed.
  01_check_connection.py    Phase 1.
  03_find_contract.py       Phase 3.
  05_paper_order.py         Phase 5 and 7. THE ONLY SCRIPT THAT SUBMITS.
                            --take-profit/--stop-loss attach a bracket,
                            --leg-tif sets leg time in force (default DAY),
                            --legs shows what is attached to an order.
                            Also --status and --cancel.
  06_positions.py           Phase 6. Read-only.

tests/                      327 tests, all offline. No network, no credentials.
```

56 Python files. Nothing in `service/` is over 700 lines.

**The seam review rule:** if any file other than `service/market/quotes.py`
names `ManualEntryProvider` or `TigerQuoteProvider`, the abstraction has
leaked and swapping to fetched data is no longer one line in `.env`. Grep 3 in
section 7 is the whole test, and it currently returns nothing.

**The import rule:** import from the folder, not the file --
`from api.service.order import buy_option`. Each `__init__.py` re-exports its
folder's public names, so moving a function between files inside a folder
breaks nothing outside it. `core/` is the exception: name the file there.


---

## 9. What is deliberately absent

- **No strategy.** Nothing decides what to trade.
- **No multi-leg or combo orders.**
- **No exercise or assignment handling.** Phase 6 warns about it; it does not
  manage it.
- **No live trading.** `assert_order_allowed` blocks non-paper outright.
  Removing that line is a separate, deliberate act.
- **No tick-size rule.** Not guessed. The limit price is typed.
- **No closing logic in Phase 6.** It reports; it does not act.

---

## 10. Rules that still apply

From section 0 of the specification. They do not lapse:

1. Do not invent API functions, parameters, or field names. Fetch the docs. If
   it cannot be verified, stop and say so.
2. Build phase by phase, and wait for explicit approval.
3. Every phase must run against the **paper** account before moving on.

Rule 3 of the original spec — no `place_order` before Phase 5 — has been
satisfied and superseded. Phase 5 is complete, and the call now exists in
exactly one place, `orders.py`, behind the three locks and the typed cash
confirmation.

### If you pick this up next

Nothing is outstanding. Reasonable next steps, in rough order of value:

1. Buy `usOptionQuote` and write `TigerQuoteProvider` (§2). It is the only
   purchase still worth making, and no file outside `service/market/quotes.py` should
   change.
2. Watch the two live brackets. The DAY legs on the 360 call expire at the
   close of the US trading day; the GTC legs on the 370 call should survive it.
   A free, direct confirmation of §3a if you check them tomorrow.
3. If a second multi-contract SELL ever happens, compare its commission
   against `2.985 + 0.035q` (§3). The one extra cent on the 3-contract sell is
   the only thing in the fee model still unexplained, and a proceeds-based
   regulatory fee is the obvious candidate.
