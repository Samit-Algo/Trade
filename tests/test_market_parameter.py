"""Every endpoint that is about a market lets the caller say which one.

An endpoint that forgot would silently answer for the US whatever the page
had selected -- India's switch would arm the US, India's symbols would show
US positions. So this is checked against the published API, not by hand.
"""

from __future__ import annotations

#: Endpoints that are about the service, not about any one market.
NOT_ABOUT_A_MARKET = {"/health", "/markets"}


def takes_a_market(operation: dict, spec: dict) -> bool:
    """Whether an operation accepts `market`: as a query parameter, in its
    body, or as the path itself -- /markets/{market_id}/switch."""
    if any(p.get("name") in ("market", "market_id") for p in operation.get("parameters", [])):
        return True

    body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    ref = body.get("schema", {}).get("$ref", "")
    if ref:
        model = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        return "market" in model.get("properties", {})
    return False


def test_every_market_endpoint_takes_a_market(fake_env):
    from backend.main import create_app

    spec = create_app().openapi()

    missing = [
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        if path not in NOT_ABOUT_A_MARKET
        for method, operation in operations.items()
        if not takes_a_market(operation, spec)
    ]
    assert missing == [], "No way to say which market:\n" + "\n".join(missing)


def test_routes_never_fall_back_to_the_us_on_their_own():
    """A bare get_market() in a route means "US" whatever was asked.

    trade.py's helpers keep one as a default for callers that pass none,
    and only there.
    """
    from pathlib import Path

    routes = Path(__file__).resolve().parent.parent / "backend" / "api" / "routes"
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in sorted(routes.glob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "get_market()" in line and path.name != "trade.py"
    ]
    assert offenders == [], "\n".join(offenders)


def test_every_call_the_page_makes_names_the_market(asset_beside):
    """A call without it would quietly be about the US, whatever is selected."""
    import re

    page = asset_beside("backend.api.routes.ui", "trade_form.html")

    calls = re.findall(r"fetch\(\s*(withMarket\()?\s*[\"`](/[^\"`?]*)", page)
    assert calls, "no fetch calls found -- has the page changed shape?"

    unnamed = sorted({
        path for wrapped, path in calls
        if not wrapped and path not in ("/health", "/markets")
        # The market is in the path itself.
        and not path.startswith("/markets/${row.dataset.id}/")
    })
    assert unnamed == [], "Calls that do not say which market: " + ", ".join(unnamed)


def test_the_page_sends_the_market_with_every_order(asset_beside):
    """POST bodies carry it, since withMarket only touches the URL."""
    page = asset_beside("backend.api.routes.ui", "trade_form.html")

    assert "market: currentMarket" in page.split("function body()")[1].split("}")[0]
    close_call = page.split('fetch(withMarket("/positions/close")')[1][:300]
    assert "market: currentMarket" in close_call
