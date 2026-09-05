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
from fastapi.responses import HTMLResponse

router = APIRouter()

#: Read per request rather than at import, so editing the page and refreshing
#: the browser is enough -- no server restart.
PAGE = Path(__file__).resolve().parent / "trade_form.html"


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def trade_form() -> HTMLResponse:
    """Serve the manual testing form.

    Kept out of the OpenAPI schema on purpose: it is a tool for a human, not
    part of the API surface a client should program against.

    Returns:
        The form page.
    """
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))
