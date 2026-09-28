"""A hand-testing form for POST /trade, served at GET /ui.

WHY IT IS SERVED FROM HERE rather than opened as a file: a page loaded from
`file://` calling `http://127.0.0.1:8000` is a cross-origin request, which the
browser blocks. Serving the page from the API itself makes it same-origin, so
no CORS permission has to be opened up on a service that can place orders.

The page is static HTML and holds no secrets. The API key is typed into the
form by whoever is using it and kept only in their own browser.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from ..errors import ApiError

router = APIRouter()

#: Read per request rather than at import, so editing the page and refreshing
#: the browser is enough -- no server restart.
PAGE = Path(__file__).resolve().parent / "trade_form.html"


#: TradingView's Lightweight Charts, vendored rather than fetched from a CDN.
#: The page loads no other external script and works with no network beyond
#: this backend; a CDN would make the chart depend on someone else's uptime
#: and give a third party a request from every trading session.
VENDOR = Path(__file__).resolve().parent / "vendor"


@router.get("/ui/vendor/{name}", include_in_schema=False)
def vendor_asset(name: str) -> Response:
    """Serve one vendored script.

    Args:
        name: The file, which must sit directly in vendor/.

    Returns:
        The script.

    Raises:
        ApiError: 404 when it is not a vendored file. The name is resolved
            and checked against the folder, so "../.env" cannot escape it.
    """
    target = (VENDOR / name).resolve()
    if target.parent != VENDOR.resolve() or not target.is_file():
        raise ApiError(
            status_code=404,
            error_code="NOT_FOUND",
            message=f"No vendored asset named {name!r}.",
        )

    return Response(
        target.read_text(encoding="utf-8"),
        media_type="application/javascript",
        # Vendored and versioned by filename, so it never needs revalidating.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def trade_form() -> HTMLResponse:
    """Serve the manual testing form.

    Kept out of the OpenAPI schema on purpose: it is a tool for a human, not
    part of the API surface a client should program against.

    Returns:
        The form page.
    """
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))
