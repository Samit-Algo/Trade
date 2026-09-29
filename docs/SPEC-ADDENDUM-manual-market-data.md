# Spec addendum — manual market data

**Status: approved for design, not yet implemented.** Phase 3 implementation is
a separate approval.

> **SUPERSEDED IN PART (2026-09-10).** Every `open_interest` mention below is
> historical. The field was removed from the quote snapshot, the typed-entry
> prompt, the liquidity check and the audit record, along with the strike
> selector that used it -- see HANDOVER.md section 3j for why. Read those
> rows as a record of what was specified, not of what the code does.

Extends `../tiger-options-backend-spec.md`. Where this document and the
original disagree, this one is newer and wins; everything it does not mention
is unchanged. In particular the three safety locks, the phase-by-phase rule,
and the ban on `place_order` before Phase 5 all stand exactly as written.

---

## 0. Why

We are not buying option market data yet. The data path becomes:

```
Tiger app (read by hand)  ->  typed in  ->  this backend  ->  Tiger paper order
```

The account cannot fetch option quotes, but it **can** look contracts up. So
the machine keeps doing the part it is good at — verifying that a contract
exists, resolving its identifier, its multiplier, its strike ladder — and the
human supplies only the four numbers the machine is not allowed to see.

The design goal is that this is a **temporary substitution behind a stable
seam**. When quote data is bought, one environment variable changes and no
other file is touched.

---

## 1. What the probe established (2026-09-03)

Contract lookup is a **separate entitlement from quote data, and this account
has it.** Verified live, no permission errors:

| Call | Result |
|---|---|
| `TradeClient.get_contract(symbol, sec_type=OPT, currency, expiry, strike, put_call)` | OK — full metadata |
| the same with an impossible strike (317.13) | **ERROR 1200 `bad_request`** — the API refuses it |
| `TradeClient.get_derivative_contracts(symbol, sec_type, expiry)` | OK — 208 contracts, 104 strikes × call/put |
| `TradeClient.get_contract` / `get_contracts` for STK | OK |

Both are 60 requests/minute. A real contract returns:

```
identifier  = 'AAPL  260918C00320000'
strike      = 320.0        multiplier  = 100.0
contract_id = 353122977    name        = 'Apple'
```

This matters more than it first appears: **the strike check is real, not
advisory.** The exchange's own answer decides whether a contract exists, so a
mistyped strike fails at lookup rather than surviving to become an order.

`option_contract_by_symbol()` and `option_contract()` are *pure local
constructors*. They build a `Contract` with no network call and validate
nothing, so they will happily describe a contract that does not exist. They are
for assembling an order object, never for verification.

---

## 2. What the human types

Exactly five values per contract, all read off the Tiger app:

| Field | Why it must be typed |
|---|---|
| `bid` | no quote entitlement |
| `ask` | no quote entitlement |
| `volume` | no quote entitlement |
| `open_interest` | no quote entitlement |
| `limit_price` | `min_tick` is `None` from the API (see §9), so the valid increment cannot be derived. The app shows it. |

Everything else is fetched: contract identifier, strike, expiry, multiplier,
contract id, the strike ladder, and days-to-expiry.

**Implied volatility is not typed.** It is optional and blank by default. It is
not used to cost anything, and making it a fifth number to transcribe every
time buys nothing. Instead the order preview carries a fixed reminder:

```
IV / EARNINGS : not captured. Check the app before sending:
                - Is implied volatility unusually high for this contract?
                  You may be buying expensive time value that collapses
                  after the event, even if the direction is right.
                - Are earnings due before expiry?
```

The wording is a prompt to the human, not a calculation. Nothing in the system
reads it.

---

## 3. The interface

New file, `tiger_backend/providers.py`. One file, no others.
_(Phase 9 moved it to `api/service/market/quotes.py`. Still one file, no others.)_

```
QuoteSource            enum: MANUAL | TIGER_API

QuoteSnapshot          frozen dataclass
                         bid            float
                         ask            float
                         volume         int | None
                         open_interest  int | None
                         limit_price    float | None
                         source         QuoteSource   <- REQUIRED
                         captured_at    datetime      <- REQUIRED

MarketDataProvider     abstract base class, one abstract method:
                         get_quote(contract) -> QuoteSnapshot

ManualEntryProvider    implements it by prompting
TigerQuoteProvider     implements it later via get_option_briefs
```

Note the name: **`QuoteSnapshot`, not `ContractQuote`.** `market.py` already
defines a `ContractQuote` for the `get_option_briefs` response, and two types
meaning almost the same thing under one name is exactly the confusion this
document exists to prevent.

`source` and `captured_at` are **required constructor fields with no
defaults**. A snapshot cannot be built without declaring where it came from and
when. Labelling is therefore structural — it cannot be forgotten, because the
code will not run without it.

An abstract base class is used rather than `typing.Protocol`: one level of
inheritance is not deep, and `raise NotImplementedError` documents the contract
to whoever maintains this next.

---

## 4. Wiring, so swapping providers touches one file

Everything that needs a quote **takes a provider as a parameter**. Nothing
imports a concrete provider except the factory.

```
providers.py     the ONLY file that knows both implementations exist
                 build_market_data_provider(settings) -> MarketDataProvider

.env             MARKET_DATA_SOURCE=manual        the switch, one line
                 (accepted values: manual | tiger)
```

`pricing.py`, `orders.py`, and every script accept a `MarketDataProvider` and
never name a concrete class.

**The review rule:** if any file other than `providers.py` imports
`ManualEntryProvider` or `TigerQuoteProvider` by name, the seam has leaked and
the change should be rejected. That single grep is the whole test of whether
this design is holding.

`MARKET_DATA_SOURCE` is parsed by `config.py` in the same strict style as the
existing booleans — an unrecognised value raises `ConfigError` rather than
quietly defaulting, because silently falling back to manual entry when someone
meant live data would be its own kind of wrong.

---

## 5. Labelling and staleness

### Labelling, in four layers

One layer is not enough, because each fails differently.

1. **In the type.** `source` on every `QuoteSnapshot`, required. Cannot be
   omitted.
2. **Inline.** `[MANUAL]` printed next to every price wherever it appears.
3. **In the order preview.** A loud block when any input is manual, in the same
   visual register as the LIVE-account banner:

   ```
   ------------------------------------------------------------
   !!  PRICES BELOW WERE TYPED BY HAND, NOT FETCHED  !!
   !!  Entered 14:32:07, 23 seconds ago                      !!
   !!  Nothing has checked them against the live market.      !!
   ------------------------------------------------------------
   ```
4. **In the audit log.** Source recorded per order, so a post-mortem can tell
   the difference. See §7.

### Staleness

A manual quote is stale the moment it is typed. You read the app, type five
numbers, think, confirm — a minute passes and the market has moved. Fetched
data does not have this problem, so it needs designing in rather than assuming.

- `captured_at` is stamped when entry completes.
- `--max-quote-age` defaults to **60 seconds**, configurable per run.
- When a snapshot is used past that age, **re-prompt**. Do not warn and
  proceed; a warning that can be scrolled past is not a control.
- The age at time of use is printed in the preview and recorded in the log.

---

## 6. Validation of typed input

### Ordinary checks — reject or y/n confirm

| Check | Action |
|---|---|
| `bid >= 0` | reject, re-prompt |
| `ask > 0` | reject, re-prompt |
| `limit_price > 0` | reject, re-prompt |
| `ask > bid` | reject, re-prompt |
| `ask == bid` | suspicious, y/n confirmation |
| `volume`, `open_interest` non-negative integers | reject, re-prompt |
| spread > ~50% of ask | y/n confirmation, showing the computed spread |
| `limit_price` outside `[bid, ask]` | y/n confirmation — legal, but usually a mistake |

Rejections re-prompt for that field alone. There is no reason to retype four
correct numbers because the fifth was wrong.

### The decimal-slip check — its own failure mode

This one is **not** one confirmation among several, because it catches the
error that costs the most: a misplaced decimal point turning $520 into $5,200.

`get_option_bars` works without any entitlement (verified 2026-09-03), so the
contract's most recent daily close is available for free. Compare it to the
typed `ask`:

- ratio > **3.0** or < **0.33** → stop and demand an override

```
============================================================
  TYPED PRICE IS 10.6x THE LAST TRADED PRICE
============================================================
  You typed         : 52.00
  Last traded close : 4.90   on 2026-08-28  (6 days ago)
  Ratio             : 10.6x

  A misplaced decimal point is the most likely explanation.
  At 100 shares per contract this is the difference between
  $490 and $5,200.

  If 52.00 is genuinely correct, retype it as:  USE 52.00
============================================================
>
```

The override is the **value retyped inside a phrase** — `USE 52.00` — so it
cannot be cleared by a reflexive `y`. A wrong number cannot be confirmed
without writing the wrong number out again deliberately.

Two conditions to handle honestly:

- **No bars available** (a contract that has never traded): skip the check and
  print that it was skipped and why. Silence would imply it passed.
- **A stale last close.** On a thin contract the last trade may be days old, so
  the age is always shown. This is a sanity check, never a price source, and
  the output says so.

---

## 7. Audit log

Section 4 of the original spec already logs timestamp, contract, action,
quantity, limit price, computed cash, resolved mode, order ID and fill outcome.
Add to every order record:

| Field | Purpose |
|---|---|
| `quote_source` | `MANUAL` or `TIGER_API` |
| `typed_bid`, `typed_ask`, `typed_volume`, `typed_open_interest` | the four market values as typed |
| `quote_captured_at` | when they were entered |
| `quote_age_seconds` | how stale they were when the order went out |
| `last_close_at_entry`, `last_close_ratio` | what the decimal-slip check saw |

The existing `limit_price` field stays where it is, marked as typed when the
source is `MANUAL`.

**Why:** when a fill comes back wrong, the only question that matters is
whether the market moved or the human mistyped. Without the typed values
recorded next to the fill, that is unanswerable after the fact. With them it is
a two-line comparison. The account number stays masked, as now.

---

## 8. What Phase 3 becomes

Because contract lookup works, **Phase 3 is fully verifiable today** — no
purchase, and no manual entry at all, because contract resolution never needs
bid or ask.

`find_option_contract(underlying, option_type, strike, expiry)`:

1. Validate `option_type` is CALL or PUT. Unchanged.
2. Verify the expiry against `list_expirations`. Unchanged, **including the
   expired-expiry rule already carried in `HANDOVER.md` §6**: a listed-but-past
   date must be refused as *expired*, not as *not found*.
3. Verify the contract with `TradeClient.get_contract(...)`. The API refuses a
   bad strike itself, so this is real validation rather than a local guess.
4. On failure, build the "nearest available strikes" message from
   `TradeClient.get_derivative_contracts(...)` — sorted numerically, after the
   string-to-float conversion in §9.

The returned dataclass splits cleanly by origin, and the split is visible in
the code rather than implied:

| From the API | From the provider |
|---|---|
| identifier, underlying, expiry (both formats), strike, put_call, multiplier, contract_id, days_to_expiry | bid, ask, volume, open_interest, limit_price, source, captured_at |

`implied_volatility` stays on the dataclass as `None` under manual entry.

**Phase 3 verification becomes:** resolve a CALL, resolve a PUT, and resolve one
deliberate failure — as originally specified — plus one new case, a strike that
is real for a different expiry but not for this one, which the API is now able
to reject for us.

---

## 9. Four SDK quirks that must be handled explicitly

Each needs a comment at its handling site saying *why*, because every one of
them fails silently or misleadingly.

**1. `strike` is a string on the ladder, a float on the single lookup.**
`get_derivative_contracts` returns `strike='50.0'`; `get_contract` returns
`strike=320.0`. Convert to float on the way in.
*Why it matters:* string sorting puts `'100.0'` before `'95.0'`, so a
"nearest available strikes" message built without converting would list the
wrong strikes while looking perfectly reasonable.

**2. `identifier` and `name` swap meaning between the two calls.**
On `get_contract`: `identifier='AAPL  260918C00320000'`, `name='Apple'`.
On `get_derivative_contracts`: `identifier=None`, and the OCC code is in
`name='AAPL  260918C00050000'`.
*Why it matters:* reading `identifier` off a ladder entry yields `None`, which
then flows onward as a missing contract rather than an obvious error.

**3. An unlisted expiry returns an empty list, not an exception.**
`get_derivative_contracts(..., expiry='20260917')` returned `[]`.
*Why it matters:* an empty result reads as "no strikes exist" when the real
answer is "that expiry does not exist". The two need different messages, so
emptiness must be checked explicitly and reported as a bad expiry.

**4. `min_tick` is `None` on both calls.**
*Why it matters:* the tick-size normaliser in Phase 4 has no API source for the
valid price increment. Rather than guess a convention, the limit price is a
typed input (§2). Revisit when a real provider arrives — a live quote feed may
carry tick information that contract metadata does not.

---

## 10. Deliberately not decided here

- **The tick-size rule itself.** Not guessed, not assumed. If a future phase
  needs to compute valid increments rather than accept them, the rule comes
  from Tiger's documentation at that point.
- **`TigerQuoteProvider`.** Specified as an interface only. It gets written
  when the entitlement is bought, and writing it must not change any file
  except `providers.py`.
- **Multi-contract entry.** One contract at a time. Batch entry multiplies the
  chance of transcribing a number onto the wrong row.

---

## 11. Approval state

Approved for design on 2026-09-03, with IV left untyped, the limit price added
as a typed input, staleness at 60 seconds, the decimal-slip check promoted to
its own override-phrase failure mode, and the typed values added to the audit
log.

**Implementation of Phase 3 is a separate approval and has not been given.**
