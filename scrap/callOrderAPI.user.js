// ==UserScript==
// @name         TradingView -> Tiger Options Backend
// @match        https://www.tradingview.com/chart/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

// Paste TIGER_API_KEY from .env here.
const TIGER_API_KEY = "PASTE_YOUR_KEY";

const API = "http://127.0.0.1:8000";

// Filled from GET /trade/settings on load. Empty until that answers, so a
// click before then reports no symbol rather than trading the wrong one.
var knownSymbols = [];

function loadSymbols() {
  GM_xmlhttpRequest({
    method: "GET",
    url: API + "/trade/settings",
    headers: { "X-API-Key": TIGER_API_KEY },
    onload: function (r) {
      try {
        var s = JSON.parse(r.responseText);
        knownSymbols = s.trade_symbols || [];
        console.log("Tradable symbols:", knownSymbols.join(", "));
      } catch (e) {
        console.error("Could not read /trade/settings:", r.status, r.responseText);
      }
    },
    onerror: function (e) {
      console.error("Could not reach the backend for the symbol list.", e);
    },
  });
}

// One helper so the key and the JSON handling are written once.
function call(method, path, body, onDone) {
  GM_xmlhttpRequest({
    method: method,
    url: API + path,
    headers: {
      "Content-Type": "application/json",
      // Required: every route but a small allowlist is refused without this.
      "X-API-Key": TIGER_API_KEY,
    },
    data: body ? JSON.stringify(body) : undefined,
    onload: function (r) {
      var parsed = null;
      try { parsed = JSON.parse(r.responseText); } catch (e) { /* not JSON */ }
      onDone(r.status, parsed, r.responseText);
    },
    onerror: function (e) {
      console.error("Request failed -- is the backend running?", e);
    },
  });
}

function callOrderAPI(type){
  var symbolTxt = $($(".chart-container")[1]).text();

  // First match wins. The list comes from TRADE_SYMBOLS in .env, fetched
  // once on load -- it used to be hardcoded here AND in the /ui dropdown,
  // and the two had already drifted apart.
  var symbol = "";
  for (var i = 0; i < knownSymbols.length; i++) {
    if (symbolTxt.indexOf(knownSymbols[i]) > -1) { symbol = knownSymbols[i]; break; }
  }

  if (!symbol) {
    console.error(knownSymbols.length
      ? "No tradable symbol on this chart. TRADE_SYMBOLS holds: " + knownSymbols.join(", ")
      : "The symbol list has not loaded yet -- is the backend running?");
    return;
  }

  // The price picks the STRIKE, so it is fetched fresh per trade rather than
  // hardcoded. GET /spot serves Yahoo's price: Tiger's own feed is ~15min
  // stale without the usStockQuote entitlement, which is wide enough to
  // choose a different strike. See api/service/market/spot.py.
  call("GET", "/spot/" + encodeURIComponent(symbol), null, function (status, spot) {
    if (status !== 200 || !spot) {
      console.error("No price for " + symbol + " (" + status + ") -- nothing sent.");
      return;
    }

    // Outside market hours this is the last trade being held, not a currently
    // trading price. Still usable, but worth seeing before it picks a strike.
    if (!spot.is_live) {
      console.warn("Price is NOT live: " + spot.note);
    }

    console.log("Placing " + type + " on " + symbol + " at " + spot.price +
                " (" + Math.round(spot.age_seconds) + "s old)");

    call("POST", "/trade", {
      // Required: 8-64 chars, unique per intended trade. The schema sets
      // extra="forbid" and rejects a request without it.
      client_order_id: "tv-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6),
      symbol: symbol,
      option_type: type,
      current_price: spot.price,
    }, function (status, data, raw) {
      console.log("Order API response:", status, data || raw);
    });
  });
}

// Exported to the page's real window so the console can reach it -- the
// script itself runs in Tampermonkey's separate sandbox.
unsafeWindow.callOrderAPI = callOrderAPI;

loadSymbols();
