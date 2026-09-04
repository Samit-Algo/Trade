# Handover

**All six spec phases complete, plus Phase 7 (attached orders), 2026-09-03.**
Everything below is verified, not assumed. Read this before touching the code so nothing gets re-derived.

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

234 unit tests pass, all offline — no network, no credentials:

```bash
python -m pytest tests/ -q
```

Commits, newest first:

```
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

Without it `scripts/02_show_chain.py` cannot print a live chain, and quotes are
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
`providers.py`. **No other file should change** — if one does, the seam has
leaked. Until then that setting raises `NotImplementedError` with a message
pointing at the unbought entitlement, rather than silently falling back.

Then re-check `02_show_chain.py` against live data: row widths, no `nan`, the
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
- `orders.estimate_round_trip_commission()` returns a flat $6.04 from the
  original single observation. Given the fee is `2 x (2.985 + 0.035q)`, that is
  correct at one contract and understates slightly as size grows — by seven
  cents at three contracts, thirty-five at ten. Accurate enough for a warning,
  and it is labelled an estimate wherever it prints.
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

## 4. Environment facts

Not derivable from the repo, because `.env` and `secrets/` are gitignored.

| Fact | Value |
|---|---|
| Licence | **TBSG** (Singapore). `TIGER_LICENSE=TBSG` in `.env`. TBSG does **not** need the `tiger_openapi_token.properties` file TBHK requires. |
| Paper account | 17 digits, ends `6574`. In both `TIGER_ACCOUNT` and `TIGER_PAPER_ACCOUNT`, identical — that is Lock 1. |
| Private key | `secrets/tiger_private_key.pem`, PKCS#8, **headerless**. |
| Market data source | `MARKET_DATA_SOURCE=manual` |
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

`--grab` is available on `00_check_capabilities.py`, `02_show_chain.py`,
`03_find_contract.py`, `04_simulate_order.py`, `05_paper_order.py` and
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
`tiger_backend/throttle.py`, one `RateLimiter` per endpoint.

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
`07_premium_history.py` possible.

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

## 7. The three locks, and how they were exercised

| Lock | Mechanism | State |
|---|---|---|
| 1 — Account allowlist | configured account must equal `TIGER_PAPER_ACCOUNT` | fails closed |
| 2 — Live opt-in | `TIGER_ALLOW_LIVE` must be `true` for any other account | `false` |
| 3 — Dry run | `DRY_RUN` must be `false` for an order to be sent | `true` |

`assert_order_allowed` is called **twice** in the order path: once as the gate
before the human is asked anything, and again immediately before `place_order`,
so nothing between the gate and the wire can have changed the mode.

All three verified live. Lock 1 refuses a non-paper account ID; Lock 3 blocked
the real order flow before the confirmation prompt when `DRY_RUN=true`.

Confirmation is **the cash amount typed exactly**, not a yes. Tests assert that
`y` does not confirm.

Two related guards, both implemented and tested:

- **Expired expiries** are refused as `EXPIRED`, never as "not found", and the
  error names the next tradable date. Reporting an expired contract as missing
  sends the reader hunting for a typo in a date that is real and was tradable
  yesterday.
- **The decimal-slip check** blocks a typed price more than 3x or less than
  0.33x the last traded close, and demands the value retyped inside an override
  phrase (`USE 115.00`) that a reflexive `y` cannot clear.

---

## 8. Where things are

```
tiger_backend/
  safety.py      Three locks, account masking, startup banner. Pure logic.
  config.py      .env -> frozen Settings. Fails closed.
  clients.py     QuoteClient and TradeClient, built separately on purpose.
  throttle.py    RateLimiter + one instance per endpoint. All limits live here.
  market.py      Phase 2. All pandas access lives here; nothing else touches it.
  contracts.py   Phase 3. Contract identity only, no market data.
  providers.py   The market-data seam. THE ONLY file naming a concrete provider.
  pricing.py     Phase 4. Pure arithmetic, no network.
  orders.py      Phase 4 build + preview, Phase 5 submit + poll.
  positions.py   Phase 6. Read-only, values at the bid.
  audit.py       JSONL order trail in logs/ (gitignored).

scripts/
  00_check_capabilities.py  Diagnostic, outside the phases. Read-only probe of
                            every endpoint. Run after buying market data to see
                            exactly what changed.
  01_check_connection.py    Phase 1.
  02_show_chain.py          Phase 2. Blocked on usOptionQuote.
  03_find_contract.py       Phase 3.
  04_simulate_order.py      Phase 4. Sends nothing.
  05_paper_order.py         Phase 5 and 7. THE ONLY SCRIPT THAT SUBMITS.
                            --take-profit/--stop-loss attach a bracket,
                            --leg-tif sets leg time in force (default DAY),
                            --legs shows what is attached to an order.
                            Also --status and --cancel.
  06_positions.py           Phase 6. Read-only.
  07_premium_history.py     Learning tool, outside the phases. Daily traded
                            prices for one contract via the free get_option_bars.

tests/                      213 tests, all offline.
  chain_fixture.py          A deliberately awkward synthetic chain. Run directly:
                            python tests/chain_fixture.py --all
```

**The seam review rule:** if any file other than `providers.py` imports
`ManualEntryProvider` or `TigerQuoteProvider` by name, the abstraction has
leaked. One grep is the whole test, and it currently passes.

A note on `tests/chain_fixture.py`: it is deliberately lopsided — volume from
12 to 1,204,553, an IV smile, uneven strike spacing, rows with no bid, one with
no ask, a call with no matching put. That is not decoration. The first, uniform
version hid a real bug: an 8-wide volume column that fitted `71,626` but not
`1,204,553`, so busy rows ran the volume into the spread column. Simplifying
the fixture makes that class of bug invisible again.

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
   purchase still worth making, and no file outside `providers.py` should
   change.
2. Watch the two live brackets. The DAY legs on the 360 call expire at the
   close of the US trading day; the GTC legs on the 370 call should survive it.
   A free, direct confirmation of §3a if you check them tomorrow.
3. Consider raising `orders.estimate_round_trip_commission()` from its flat
   $6.04 to the measured `2 x (2.985 + 0.035q)` (§3). It only matters above a
   few contracts, and the current value is labelled an estimate, so this is
   tidying rather than a fix.
