# How this project is put together

One server, one page, several markets. Today: **US** (Tiger) and **India**
(NIFTY through OpenAlgo / Angel One). Forex can be added later the same way.

Every request names its market (`?market=US` or `?market=IN`). The page has a
market selector at the top; everything below it works the same for each market.

---

## The one idea

```
   the page (/ui)          TradingView / curl
         │                        │
         └──────────┬─────────────┘
                    ▼
          backend/api/routes/*.py        one file per endpoint group
                    │   asks "the market" -- never a broker
                    ▼
          backend/markets/base.py        what every market must answer
             │                   │
             ▼                   ▼
     markets/us/           markets/india/
     (Tiger SDK)           (OpenAlgo client)
```

A route never talks to a broker. It asks a `Market` ("what is held?", "place
this order") and every market answers in the same shapes. That is why the
page and the endpoints are the same for every market.

---

## Folders

```
backend/
  main.py                 builds the app, checks the API key, starts background work
  api/
    routes/               the endpoints, and trade_form.html (the page)
    schemas.py            request and response shapes
    shared.py             the market list: get_market("US") / get_market("IN")
  core/                   things every market uses
    config.py             reads the settings files (see config/ below)
    paths.py              where logs/ and state/ are -- the only place that knows
    safety.py             the checks run just before any order is sent
    armed.py              the SAFE / ARMED switch
    symbol_settings.py    per-symbol on/off and brackets, saved from the page
    time_brackets.py      take-profit / stop-loss by time of day
  services/               the maths, the same for every market
    order/                pricing, brackets, the one-trade-per-symbol lock, the trade chart
    market/               prices, and the price recorder (logs/prices/)
    export/               the Excel export
    analysis.py           the Analysis page numbers
  markets/
    base.py               the interface every market fills in
    us/                   Tiger. us/submit.py is the ONLY file that sends US orders.
    india/                OpenAlgo. india/submit.py is the ONLY file that sends India orders.
                          india/exits.py watches the take-profit and runs the exits.

config/                   settings, one file per market (gitignored)
  server.env              the API key, host, port, and MARKETS (US or US,IN)
  us.env                  US settings
  india.env               India settings. Needed when MARKETS lists IN.
  *.env.example           the same files, blank, with every setting explained

state/                    what the page saved, one folder per market (gitignored)
  us/  in/                armed switch, symbol settings, time brackets; in/exits.json

logs/                     (gitignored)
  api_requests.log        every request that could move money
  order_audit.log         every order sent
  prices/us/  prices/in/  the price of each held contract, for the trade chart

secrets/                  the Tiger private key (gitignored). unused/ holds old keys.
tests/                    pytest; no network, no broker needed
docs/                     HANDOVER.md (why things are the way they are), specs;
                          archive/ holds old plans kept for history
```

---

## Turning a market on and off

- `MARKETS` in `config/server.env` decides which markets run on this machine.
  `MARKETS=US` never starts India. Without the line, India runs when
  `config/india.env` exists.
- A market other than the US can be switched off on the Settings page
  (`PUT /markets/IN/switch`), saved in `state/in/market_switch.json`. Off, its
  broker is not contacted and its trades are refused. Switching off is refused
  while the backend is still watching one of its trades' take profit.
- India's prices are recorded only in NSE hours (Mon-Fri 09:15-15:30 IST).

## How US and India differ

|                     | US (Tiger)                         | India (OpenAlgo)                              |
|---------------------|------------------------------------|-----------------------------------------------|
| Quantity            | contracts (1 = 100 shares)         | lots (1 lot = 65 NIFTY)                       |
| Stop-loss           | attached to the order at Tiger     | an SL order placed after the entry fills      |
| Take-profit         | attached to the order at Tiger     | watched by the backend, sold when reached     |
| Test mode           | Tiger paper account                | OpenAlgo Analyze mode                         |

---

## Adding a new market (for example Forex)

1. Make `backend/markets/<name>/` with a class that fills in `markets/base.py`.
2. Put the only order-sending code in `<name>/submit.py`, and run the safety
   check right before the broker call.
3. Add `config/<name>.env` and its `.example`.
4. Register it in `backend/api/shared.py`, and add its id to `KNOWN_MARKETS`
   in `backend/core/config.py`.

The routes, the page and the services need no change.
