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
| 7 | Attached take-profit and stop-loss (new, outside the spec) | **done** | Two bracketed orders placed live, one with DAY legs and one with GTC. Both filled; all four legs confirmed live. See §3a. |

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

## 3. Commission is 10.8% of a cheap option, and it changes break-even

**The single most surprising finding of the build.** Measured, not estimated,
from the real order:

```
premium paid       $28.00     (1 contract, filled at 0.2800)
all-in cost basis  $31.02     (average_cost 0.3102 per share x 100)
commission         $3.02      = 10.8% of the premium
```

`average_cost` is reported **per share and including commission**. The $28.00
that appeared in the fill report is not what the position cost.

What this means in practice:

- The bid must reach **0.3102**, not 0.2800, before the position is level.
  That is **+10.8%** before any profit exists at all.
- Selling costs commission again. Round trip on this position is roughly
  **$6.04, about 22%** of a $28 trade.
- The Phase 4 break-even line is computed from the limit price and therefore
  **excludes commission**. On a $1,160 order that is noise. On a $28 order it
  is most of the position.

### The round trip, now measured rather than estimated

The Phase 5 position was closed on 2026-09-03 to make way for a bracketed
order. It bought at 0.28 and sold at 0.28 — **the price did not move at all**:

```
BUY  44506652990393344   filled 0.2800   commission $3.02
SELL 44506900356154368   filled 0.2800   commission $3.02
                                         realized_pnl -$6.04
```

**The entire loss was commission.** $6.04 on a $28 premium is **21.6%**, paid
for a trade that was flat. `estimate_round_trip_commission()` predicted exactly
$6.04.

**Still one contract, though.** $3.02 appeared on both a buy and a sell, so it
is confirmed for a 1-contract order in each direction — but flat-per-order and
per-contract are still indistinguishable, because both orders were 1 contract.
A multi-contract order is what separates them. If it is flat, cheap contracts
are disproportionately punished and the practical floor for a sensible trade is
far above $28.

Phase 6 uses `average_cost`, so its P&L is already net of entry commission.
Phase 4's estimate is not.

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
| Open positions | 1x `AAPL  260918C00360000` (DAY legs live) and 1x `AAPL  260918C00370000` (GTC legs live) |

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

1. **Place a MULTI-CONTRACT order** to establish whether the $3.02 commission
   is flat per order or per contract (§3). Both samples so far were 1 contract,
   which cannot separate the two. It decides what a sensible minimum trade is.
2. Buy `usOptionQuote` and write `TigerQuoteProvider` (§2).
3. Watch the two live brackets. The DAY legs on the 360 call expire at the
   close of the US trading day; the GTC legs on the 370 call should survive it.
   That is a free, direct confirmation of §3a if you check them tomorrow.
