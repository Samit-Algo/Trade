// ==UserScript==
// @name         TradingView -> Tiger Options Backend
// @match        https://www.tradingview.com/chart/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

// Paste TIGER_API_KEY from .env here.
const TIGER_API_KEY = "PASTE_YOUR_KEY";

function callOrderAPI(type){
  var symbolTxt = $($(".chart-container")[1]).text();

  // First match wins. The earlier version reassigned on every line, so each
  // result was thrown away by the next and only the last line could ever
  // stand -- add a symbol to this list to trade it.
  var symbol = "";
  var known = ["TSLA", "QQQ", "NVDA"];
  for (var i = 0; i < known.length; i++) {
    if (symbolTxt.indexOf(known[i]) > -1) { symbol = known[i]; break; }
  }

  if (!symbol) {
    console.error("No known symbol found on the chart -- nothing sent.");
    return;
  }

  // The price that picks the strike. Still hardcoded: edit before each trade,
  // because a stale number here buys a different contract than intended.
  var current_price = 717.24;

  GM_xmlhttpRequest({
    method: "POST",
    url: "http://127.0.0.1:8000/trade",
    headers: {
      "Content-Type": "application/json",
      // Required: every route but a small allowlist is refused without this.
      "X-API-Key": TIGER_API_KEY,
    },
    data: JSON.stringify({
      // Required: 8-64 chars, unique per intended trade. The schema sets
      // extra="forbid" and rejects a request without it.
      client_order_id: "tv-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6),
      symbol: symbol,
      option_type: type,
      current_price: current_price,
    }),
    onload: function (response) {
      console.log("Order API response:", response.status, response.responseText);
    },
    onerror: function (error) {
      console.error("Order API error:", error);
    },
  });
}

// Exported to the page's real window so the console can reach it -- the
// script itself runs in Tampermonkey's separate sandbox.
unsafeWindow.callOrderAPI = callOrderAPI;
