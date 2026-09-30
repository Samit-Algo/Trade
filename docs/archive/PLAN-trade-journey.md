# Plan — the life of one contract, with a chart

## The problem

Everything recorded today happens at **entry**. `logs/order_audit.log` holds 445
records and every one is a snapshot of the moment an order was built: the
contract, the price, the bracket, the quote it was priced from. Nothing
records what the contract *did* afterwards.

So a trade that went the right way and came back is indistinguishable from
one that never moved. The example that prompted this: bought at 0.97, take
profit set at 1.05, price reached 1.03 and fell away. Today nothing anywhere
shows the 1.03 — only that the position is still open.

That matters beyond curiosity. "How often did price get most of the way to my
take profit before reversing?" is the question that tells you whether the
take profit is set too far out, and right now it cannot be asked.

## What makes this cheap

Tiger serves **one-minute OHLC bars for options**, and the client already
imports `BarPeriod` (`api/service/market/data.py:696`). Verified against this
account:

- `get_option_bars(..., period=BarPeriod.ONE_MINUTE)` returns 300 rows with
  `open, high, low, close, volume`
- it accepts `begin_time`, `end_time` and `limit`, so a request can be cut to
  exactly one trade's life rather than taking whatever 300 bars come back
- a 40-minute window around a real entry returned 15 bars, high 3.46 against
  an entry of 3.15 — **the peak this feature exists to show**
- bars still come back for a contract that expired weeks ago, so past trades
  can be charted, not just open ones

`high` is the whole answer to the question. The peak the option reached is a
column in data already on the wire.

## Scope

**Read-only, on demand.** Open a trade in Order History, get its chart. No
background polling, no new loop, nothing that runs on a timer. The bars are
fetched when the panel is opened and cached.

This is deliberate. Continuous monitoring is a much larger change and a much
riskier one — the service has no background task today, and adding one that
touches live positions is a separate decision. Minute bars after the fact
answer the same question for a fraction of the cost.

**One trade at a time.** Not a portfolio dashboard.

## What the chart shows

A candlestick (or line) of the contract's premium over the trade's life, with
four reference lines drawn across it:

| Line | Value | Why |
|---|---|---|
| Entry | the fill price | the anchor everything is measured from |
| Take profit | entry × (1 + tp%) | the level being aimed at |
| Stop loss | entry × (1 − sl%) | the level being defended |
| Peak | max(high) after entry | **the near-miss** |

Plus a marker at the entry bar, and at the exit bar when the position closed.

Under the chart, the numbers that answer the question directly:

- **Best premium reached** and when — `6.80` at `20:48`
- **How close it came** — "reached 96% of the way to take profit"
- **Worst drawdown** and when
- **Unrealised high-water P&L** against realised P&L, so a trade that was up
  $40 and closed at $6 says so

## Where the data comes from

Two sources, joined on the contract identifier:

1. **`logs/order_audit.log`** — already holds entry price, bracket
   percentages, quantity, multiplier, the resolved bracket prices and the
   bracket source strings. `api/routes/export.py` already parses this file,
   so the reader exists.
2. **Tiger minute bars** — fetched for the window from entry to exit (or to
   now, for an open position).

Nothing new is written at trade time. **The feature is pure derivation from
what is already recorded**, which is why it can be built without touching the
order path at all.

## Files

**New**

- `api/service/market/bars.py` — fetch and normalise minute bars for one
  contract over a window. Wraps `get_option_bars` the way
  `fetch_last_traded_close` (`data.py:678`) already wraps the daily call,
  including its defensive handling: an empty result comes back as a *list*,
  not an empty DataFrame, so `.empty` alone raises.
- `api/service/order/journey.py` — the derivation. Takes an audit record plus
  bars and returns the peak, the trough, when each happened, how close to the
  bracket it came, and high-water P&L. Pure function, no I/O, so the maths is
  testable without the network.
- `api/routes/journey.py` — `GET /orders/{order_id}/journey`.
- `scrap/tests/test_journey.py` — the derivation against fixed bars.

**Modified**

- `api/schemas.py` — the response models.
- `api/main.py` — register the router.
- `api/routes/trade_form.html` — the chart panel in Order History.

## The chart itself

Drawn as **inline SVG built from the bars**, not a charting library. The page
loads no external scripts today and works with no network beyond the backend;
adding a CDN dependency to draw a few hundred candles would break that for
one panel. A candlestick chart is rects and lines, and the four reference
lines are four more.

It goes in the **Order Detail** panel that already exists in Order History,
which is currently an empty box until a row is clicked.

## Details that matter

**Bars are minute bars, so a wick between them is invisible.** If price
spiked to 1.09 for four seconds inside a minute that closed at 1.02, the bar's
`high` shows 1.09 — but a spike *between* two bars is not recorded anywhere.
The panel should say it is showing minute bars rather than implying tick
resolution.

**The bracket legs are the broker's, and they fire on the broker's data.** A
take profit at 1.05 that did not fire when a bar's high reads 1.06 is not
necessarily a bug: the leg triggers on the actual quote, and a one-minute high
can come from a print the resting order never saw. Worth stating in the panel,
because it will otherwise look like a bug the first time it happens.

**Times are US/Eastern in the data.** `MARKET_TIMEZONE` already exists
(`api/service/market/data.py:124`). The panel should show both that and local
time, because you read the market in IST.

**An open position has no exit bar.** The window runs to now, the exit marker
is absent, and the peak is "so far".

**Rate limits.** `OPTION_BARS_LIMITER` already exists in
`api/service/core/broker.py` and the daily-bar call already waits on it. The
minute-bar call uses the same limiter. Results are cached per contract and
window, so reopening the same trade costs nothing.

## Verification

1. **Unit tests** on `journey.py` with handwritten bars: a trade that hit take
   profit, one that hit stop loss, one that peaked at 96% and reversed (the
   case this exists for), one still open, one with no bars at all.
2. **Boundary tests** — peak exactly at the take profit, peak in the entry
   bar itself, a single-bar trade.
3. **Against a real trade** — `QQQ 260925P00741000`, entered 2026-09-24 at
   3.15. The 40 minutes after entry are already confirmed to hold a high of
   **3.46**; the panel must show that peak and the time it happened.
4. **An expired contract** — `AAPL 260918C00320000` from 3 September, to
   confirm a closed trade still charts.
5. **No network in the test suite**, as now: bars are passed in, never fetched
   inside the derivation.

## Open questions

1. **Candlestick or line?** A line of `close` is simpler and reads fine at
   this scale; candles show the intrabar range, which is exactly what the
   near-miss is. I would do candles for that reason.
2. **Underlying price alongside?** The option's premium is what the bracket
   acts on, so the option chart is the one that answers the question. The
   underlying could be a second panel later.
3. **How far back?** Bars were returned for a contract that expired weeks ago,
   so no cutoff is needed yet. If one appears, the panel says so rather than
   showing an empty chart.
