# Handover

Paused after Phase 2, 2026-09-03. Everything below is verified, not assumed.
Read this before touching the code so nothing gets re-derived.

The build specification is `../tiger-options-backend-spec.md`. It is the
authority; this file records what has actually been done against it.

`SPEC-ADDENDUM-manual-market-data.md` extends it: option quotes are typed in
by hand from the Tiger app instead of fetched, behind a provider interface.
Approved for design 2026-09-03; not implemented.

---

## 1. Phase status

| Phase | Scope | State | How it was verified |
|---|---|---|---|
| 1 | Connect and confirm the account | **done** | Ran `scripts/01_check_connection.py` against the live paper account. Tiger reported `Account type: PAPER`, `Status: Funded`, `Capability: RegTMargin`, USD 1,000,000 available. |
| 2 | Market data: expirations and chains | **written, partly verified** | Expirations verified live: 24 real AAPL dates with correct days-to-expiry and weekly/monthly tags. The chain table is verified against a realistic synthetic fixture, **not** against live data — see §2. |
| 3 | Contract resolution | not started — **design changed**, see `SPEC-ADDENDUM-manual-market-data.md` | — |
| 4 | Cost estimation, simulated orders | not started | — |
| 5 | Paper order submission | not started | — |
| 6 | Positions and P&L | not started | — |

73 unit tests pass, all offline — no network, no credentials, no SDK needed
for most of them:

```bash
python -m pytest tests/ -q
```

Commits so far, newest first:

```
5fdead9  Fix volume column overflow, found by a realistic chain fixture
5bcfcb1  Phase 2: option expirations and chain display
fa704b0  Reject a properties file given as the private key path
9d93a39  Ignore vnv/ virtualenv directory
517f84a  Phase 1: connect and confirm the account
ee8a39e  Initial commit: project skeleton and .gitignore
```

**No order-placing code exists in the repo.** The only occurrence of the string
`place_order` is in the docstring of `assert_order_allowed` in
`tiger_backend/safety.py`, which the specification supplies verbatim.

---

## 2. The blocker: option QUOTE entitlement

Scoped deliberately: **quote** data is blocked, contract lookup is not.
See "Contract lookup is a SEPARATE entitlement" below before concluding
that anything option-related is unavailable.

`scripts/02_show_chain.py` lists expirations correctly and then fails:

```
ApiException: code=4 msg=4000:permission denied
(Current user and device do not have permissions in the US OPT quote market)
```

**This is an entitlement on the Tiger account, not a defect in this code.**

Endpoints were probed by hand on 2026-09-03, then superseded by a fuller probe.
Run `python scripts/00_check_capabilities.py` to regenerate this at any time; it
also writes `capabilities-<date>-<grab|nograb>.txt` so two runs can be diffed
across a purchase. Results as of 2026-09-03:

| Endpoint | Result | Needs paid access |
|---|---|---|
| All account/trading queries | OK | No |
| `get_quote_permission`, `get_kline_quota` | OK | No |
| `get_option_expirations` | OK | No — free |
| `get_option_bars` | **OK** | No — free |
| `get_option_timeline` | **OK** | No — free |
| `get_stock_delay_briefs`, `get_bars`, `get_market_status` | OK | No — free |
| `get_option_chain` | permission denied | Yes — `usOptionQuote` |
| `get_option_briefs` | permission denied | Yes — `usOptionQuote` |
| `get_option_depth` | permission denied | Yes — `usOptionQuote` |
| `get_option_trade_ticks` | permission denied | Yes — `usOptionQuote` |
| `get_option_analysis` | permission denied | Yes — `usOptionQuote` |
| `get_stock_briefs` | permission denied | Yes — `usStockQuote` |
| `get_trade_ticks` | permission denied | Yes — `usStockQuote` |

`get_quote_permission()` reports the account holds exactly one permission:
`aStockQuoteLv1` (China A-share L1, permanent). Nothing for US markets.

### Contract lookup is a SEPARATE entitlement, and we have it

Probed 2026-09-03. The capability probe above covers **quote** endpoints only,
which made this file read as though options were shut off altogether. They are
not.

| Endpoint | Result | Needs paid access |
|---|---|---|
| `TradeClient.get_contract` (STK and OPT) | **OK** | **No** |
| `TradeClient.get_contracts` | **OK** | **No** |
| `TradeClient.get_derivative_contracts` | **OK** | **No** |

All three are 60 requests/minute. A real option returns full metadata — identifier,
strike, multiplier, contract id, name — and `get_derivative_contracts` returns
the entire strike ladder for one expiry (104 strikes × call/put for AAPL
2026-09-18). An impossible strike is **refused** with `ERROR 1200 bad_request`,
so the validation is the exchange's answer rather than a local guess.

This is what makes the manual-market-data architecture possible: the machine
verifies contract identity for free, and only bid/ask/volume/OI/limit price
have to be read off the Tiger app by hand. See
`SPEC-ADDENDUM-manual-market-data.md`.

`option_contract_by_symbol()` and `option_contract()` are **pure local
constructors** — no network call, no validation. They will happily build a
contract that does not exist, so they are for assembling an order object, never
for verification.

### Correction to an earlier assumption in this file

Option market data is *not* entirely blocked. Historical option bars and the
intraday option timeline both work today without any purchase. Only the
real-time chain and quote endpoints need `usOptionQuote`. If a future phase
needs option prices and the entitlement is still unbought, `get_option_bars` is
a real fallback worth considering — it was not known to be available when
Phase 2 was written, and it is what the addendum's decimal-slip check uses.

Consequences already handled in the code:

- `fetch_underlying_price()` tries `get_stock_briefs`, falls back to
  `get_stock_delay_briefs`, and labels which one it used. This works today.
- The option *chain* has no free equivalent. Tiger publishes no delayed option
  endpoint — the SDK's only `*delay*` method is `get_stock_delay_briefs`,
  confirmed by reading `quote_client.py`. A full chain across all strikes
  cannot be displayed until `usOptionQuote` is bought. Per-contract history
  via `get_option_bars` is a different matter and does work.
- `02_show_chain.py` detects `permission denied` in the error text and says so
  explicitly rather than listing generic causes.

### How to unblock

Buy **OpenAPI US option market data**, via either:

- Tiger Trade app → My → market data access → OpenAPI Permissions, or
- Personal Center on the Tiger website.

Real-time OpenAPI market data is sold separately and does **not** come with a
developer account. US *option* data is separate again from US *stock* data;
buying stock data alone will not unblock the chain.

### What to re-run once it is active

No code changes should be needed.

```bash
python scripts/02_show_chain.py AAPL          # menu, then the chain table
python scripts/02_show_chain.py AAPL --all    # every strike
```

Then check against live data, since only synthetic data has exercised this:

1. Every row is the same width; no column has overflowed.
2. Nothing prints as `nan`.
3. The `> ... <` marker is on the strike nearest the live spot price.
4. Real thin rows carry the `!` flag.
5. The spot price header says `real-time`, not `delayed ~15 min`.

If the underlying price still says delayed, US **stock** market data is also
unbought — that is a separate purchase and does not block the chain.

---

## 3. Environment facts

Not derivable from the repo, because `.env` and `secrets/` are gitignored.

| Fact | Value |
|---|---|
| Licence | **TBSG** (Singapore). Set as `TIGER_LICENSE=TBSG` in `.env`. TBSG does **not** need the `tiger_openapi_token.properties` file that TBHK licences require. |
| Paper account | 17 digits, ends `6574`. Present in both `TIGER_ACCOUNT` and `TIGER_PAPER_ACCOUNT`, identical — that is Lock 1. |
| Private key | `secrets/tiger_private_key.pem`, PKCS#8, **headerless**. See below. |
| Virtualenv | `vnv/`, not `.venv`. Activate with `vnv\Scripts\activate`. |
| Python | 3.11.9. `tigeropen` 3.7.1. |
| Git | The repo root is this directory. `C:\Users\manoj` is *itself* a git repo (an accident of a Cursor worktree operation); never run `git add -A` from there. |

### The private key, and the trap in it

Tiger supplied credentials as `secrets/DEMO_tiger_openapi_config.properties`,
which holds the key inline on its `private_key_pk8=` line. The key was
extracted from there into `secrets/tiger_private_key.pem`.

**The .pem contains the raw base64 and nothing else — no header lines.**
`read_private_key()` in the SDK strips only `-----BEGIN RSA PRIVATE KEY-----`
markers. PKCS#8 headers (`-----BEGIN PRIVATE KEY-----`) would survive the strip,
stay in the string, and break request signing.

Pointing `TIGER_PRIVATE_KEY_PATH` at the `.properties` file instead of the
`.pem` costs an hour. The SDK reads the whole file verbatim and tries to sign
with all 1759 characters of it, surfacing as:

```
Unable to load PEM file ... InvalidData(Invalid symbol 95, offset 7.)
```

Symbol 95 is `_`, at index 7 — the underscore in `private_key_pk8`. `config.py`
now rejects a properties file at that path with a readable message, so this
cannot recur silently.

### Market data device access — this project overrides the SDK default

`QuoteClient(client_config, logger=None, is_grab_permission=True)` calls
`grab_quote_permission()` during `__init__` by default. That is not a purchase
and grants nothing new, but it **moves** primary-device status to this machine
and takes it from whatever held it before — the Tiger app on a phone, for
instance. Only one device holds it at a time.

Between Phase 1 and 2026-09-03 every script did this silently on every run.
**`build_quote_client` now defaults to `grab_permission=False`**, so no script
claims device access unless asked. Nothing in this project needs it.

The consequence, and the reason this section exists: a real-time call may now
fail with

```
code=4 msg=4000:permission denied(current device does not have permission)
```

**That is a different failure from an unbought entitlement**, which instead
reads `...do not have permissions in the US OPT quote market`. Telling them
apart matters, because they have different fixes:

| Message | Meaning | Fix |
|---|---|---|
| `current device does not have permission` | Another device holds primary status | Re-run with `--grab` |
| `...permissions in the US OPT quote market` | The entitlement was never bought | Buy `usOptionQuote` |

`--grab` is the deliberate fix for the first, available on
`00_check_capabilities.py`, `02_show_chain.py` and `07_premium_history.py`.
Running it takes device status back from your phone, which is why it is opt-in
rather than automatic.

### Both accepted, do not switch

Do **not** move to the SDK's `props_path` mode, even though it is fewer steps.
Lock 1 compares the account in `.env` against the declared paper account. If
the SDK sourced its account from a properties file instead, the two could drift
and the safety check would be guarding a value the SDK no longer uses.
`.env` stays the single source of truth.

---

## 4. Confirmed SDK signatures

Read off the official docs and used as written. Do not guess at these; if
something is needed that is not listed here, fetch the page.

### Account — https://docs-en.itigerup.com/docs/accounts

```python
TradeClient.get_managed_accounts(account=None, lang=None)          # 60/min
TradeClient.get_prime_assets(account=None, base_currency=None,
                             consolidated=True, lang=None)         # 60/min
```

`get_managed_accounts` returns `AccountProfile` objects with `.account`,
`.capability`, `.status`, `.account_type` (`GLOBAL` / `STANDARD` / `PAPER`).

`get_prime_assets` returns a `PortfolioAccount` with `.account`,
`.update_timestamp`, and `.segments`, a dict keyed by category — `'S'`
securities (where options live), `'C'` futures, `'F'` fund, `'D'` crypto.

### Options — https://docs-en.itigerup.com/docs/quote-option

```python
QuoteClient.get_option_expirations(symbols, market=None)                # 60/min
QuoteClient.get_option_chain(symbol, expiry, option_filter=None,
                             return_greek_value=None, market=None,
                             timezone=None, **kwargs)                   # 60/min
QuoteClient.get_option_briefs(identifiers, market=None, timezone=None)  # 120/min
```

All three return `pandas.DataFrame`, and all three take `market`. This project
passes `market=Market.US` explicitly on every one of them rather than relying
on the server default.

Expirations columns: `symbol`, `option_symbol`, `date` (already `YYYY-MM-DD`),
`timestamp` (ms), `period_tag` (`m` monthly, `w` weekly).

Chain columns: `identifier`, `symbol`, `expiry` (ms), `strike`, `put_call`,
`multiplier`, `bid_price`, `bid_size`, `ask_price`, `ask_size`, `pre_close`,
`latest_price`, `last_timestamp`, `volume`, `open_interest`, `implied_vol`,
plus the deprecated Greeks.

### Stock — https://docs-en.itigerup.com/docs/quote-stock

```python
QuoteClient.get_stock_briefs(symbols, include_hour_trading=False, lang=None)  # 120/min
QuoteClient.get_stock_delay_briefs(symbols, lang=None)                        # 10/min
```

**Neither takes a `market` parameter.** The "pass `market=Market.US`
everywhere" rule applies to the option endpoints only.

`get_stock_briefs` reports the price in `latest_price`.
`get_stock_delay_briefs` has no such column — it reports `close`.

### Rate limits

Every documented limit is enforced by `tiger_backend/throttle.py`, one
`RateLimiter` per endpoint. The limits live in that one file; change them there.

---

## 5. Three documentation gotchas

These are the ones that produce plausible wrong answers rather than errors.

**1. Two different volatilities, confusingly named.**
The option chain returns `implied_vol` — *implied* volatility, what the market
expects ahead. `get_option_briefs` returns `volatility` — *historical*
volatility, how much the underlying has actually moved. They are different
measurements and mixing them gives a number that looks reasonable and is wrong.
They are kept on separate dataclasses for this reason:
`OptionRow.implied_volatility` and `ContractQuote.historical_volatility`, with a
comment at each usage site. Do not merge them.

**2. Rate limits are not uniform.**
Expirations and chain are 60/min; `get_option_briefs` and `get_stock_briefs`
are 120/min; `get_stock_delay_briefs` is only 10/min. A single global throttle
would be both wrong and needlessly slow.

**3. The same field has two names.**
The chain calls the last trade time `last_timestamp`. `get_option_briefs` calls
it `latest_time`. Same concept, different spelling, no error if you read the
wrong one — you get `None`.

### Also worth knowing

- **Expiry timestamps are midnight US/Eastern**, not UTC. Reading one in
  another zone lands on the wrong calendar day and shifts the whole expiry.
  All conversion goes through `milliseconds_to_date()` in `market.py`.
- **Greeks are deprecated** on the option chain (`delta`, `gamma`, `theta`,
  `vega`, `rho`). They update once daily and Tiger says not to use them
  intraday. This project neither requests nor displays them, and builds no
  logic on them. Do not add them back.
- **`get_option_bars` ignores `limit`.** Measured on 2026-09-03: asking for 5
  bars returned all 61. The defaults (`begin_time=-1`, `end_time` far future)
  already return a contract's whole life, so pass no limit and trim the
  display client-side if needed.
- **`get_option_bars` returns an empty `list`, not an empty DataFrame**, when a
  contract has no history. Testing `.empty` alone raises `AttributeError` and
  buries the real answer. Check `isinstance(value, list)` first.
- **`get_option_bars` works on expired contracts.** The SDK's own docstring
  example uses one. This is how `07_premium_history.py` can show a dead
  contract's full life.
- **Four more quirks in the contract-lookup calls** — `strike` typed as str
  on the ladder but float on the single lookup, `identifier` and `name`
  swapping meaning between them, an unlisted expiry returning an empty list
  rather than an error, and `min_tick` always `None`. Each is written up with
  its failure mode in `SPEC-ADDENDUM-manual-market-data.md` §9.

---

## 6. Open item for Phase 3

**Contract resolution must reject an expired expiry with an error that says
"expired", not "not found".**

Tiger returns past dates in the expirations list. Observed on 2026-09-03:

```
  #  DATE          DAYS  TYPE
  1  2026-09-02      -1  weekly   EXPIRED
  2  2026-09-04       1  weekly
```

`2026-09-02` had already expired and was still returned as the first entry.
`scripts/02_show_chain.py` labels it `EXPIRED` in the menu, but nothing yet
*prevents* it being chosen.

So in `find_option_contract()`, the expiry check has three outcomes, not two:

1. The date is not in Tiger's list → "not a listed expiration", listing the
   nearest available dates. (Specified in Phase 3 already.)
2. The date **is** in Tiger's list but `days_to_expiry < 0` → an error saying
   the contract has **expired**, and giving the next tradable date.
3. Otherwise → resolve it.

Case 2 is the one that matters here. Reporting an expired contract as
"not found" sends the reader hunting for a typo in a date that is real and
correct, and was tradable yesterday. The error must name the actual problem.

`days_until_expiry()` in `market.py` already returns a negative number for a
past date, so the check is available; it just needs wiring in when Phase 3 is
written.

---

## 7. Where things are

```
tiger_backend/
  safety.py     Three locks, account masking, startup banner. Pure logic.
  config.py     .env -> frozen Settings. Fails closed.
  clients.py    QuoteClient and TradeClient, built separately on purpose.
  throttle.py   RateLimiter + one instance per endpoint.
  market.py     Phase 2. All pandas access lives here; nothing else touches it.
  contracts.py  Phase 3 placeholder.
  pricing.py    Phase 4 placeholder.
  orders.py     Phase 4/5 placeholder.
  positions.py  Phase 6 placeholder.

scripts/
  00_check_capabilities.py Diagnostic, outside the phase sequence. Read-only
                           probe of every endpoint; run it after buying market
                           data to see exactly what changed. Writes
                           capabilities-<date>-<grab|nograb>.txt -- the suffix
                           matters, so two reports are never compared across a
                           device-access difference they do not describe.
                           Both 2026-09-03 baselines are committed and differ
                           only in that header line: device access changes
                           nothing on an account with no US entitlements.
  01_check_connection.py   Phase 1, working.
  02_show_chain.py         Phase 2, display only. Blocked on entitlement.
  03..06_*.py              Placeholders.
  07_premium_history.py    Learning tool, outside the phase sequence. Daily
                           traded prices for one contract, via the free
                           get_option_bars. Standalone: Phases 3-6 do not
                           import it, and it does not price orders.

tests/
  chain_fixture.py     A deliberately awkward synthetic chain. Run it directly
                       to see the table: python tests/chain_fixture.py --all
  test_chain_table.py  Layout: row widths, no "nan", ATM marker, thin flags.
  test_market.py       Time conversion, spreads, liquidity, strike pairing.
  test_config.py       .env parsing.
  test_safety.py       The three locks.
```

A note on `tests/chain_fixture.py`: it is deliberately lopsided — volume from
12 to 1,204,553, an IV smile, uneven strike spacing, rows with no bid, one with
no ask, and a call with no matching put. That is not decoration. The first,
uniform version of it hid a real bug: an 8-wide volume column that fitted
`71,626` but not `1,204,553`, so busy rows ran the volume into the spread
column beside it. If this fixture is ever simplified, that class of bug becomes
invisible again.

---

## 8. Rules that still apply

From section 0 of the specification, and they do not lapse:

1. Do not invent API functions, parameters, or field names. Fetch the docs.
   If it cannot be verified, stop and say so.
2. Build phase by phase. Stop at the end of each and wait for explicit
   approval. Do not read ahead and implement later phases early.
3. No code calling `place_order` before Phase 5 — not commented out, not
   behind a flag, not in a docstring example.
4. Every phase must run against the **paper** account before moving on.

Phase 3 has been specified but **not** approved to start.
