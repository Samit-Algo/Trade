"""The FastAPI application: the fourth lock, error handling, and startup.

A second entry point over `tiger_backend`, not a rewrite. The CLI scripts in
`scripts/` remain the verified evidence behind HANDOVER.md and keep working;
both call the same library functions.

**The fourth lock.** An HTTP port that can place orders is a different risk
from a CLI. Assume anything that can reach the port will try. So:

  - every request must carry a matching `X-API-Key` header, or it is refused
    with 401 before it reaches a route
  - the service refuses to start at all without a key configured
  - it binds to 127.0.0.1 by default

The three original locks are untouched, and `assert_order_allowed` still runs
twice on every order path inside `orders.py`.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from tiger_backend.config import ConfigError

from .deps import get_settings
from .errors import ApiError, classify_exception
from .routes import contracts, health, market, orders, positions

#: Paths reachable without a key. Deliberately tiny: only the liveness check,
#: and the docs, which describe the API without exposing account data.
UNPROTECTED_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})

API_KEY_HEADER = "X-API-Key"


def create_app() -> FastAPI:
    """Build the application.

    Returns:
        The configured FastAPI app.

    Raises:
        ConfigError: If no API key is configured. The service refuses to start
            rather than serve an unauthenticated order endpoint.
    """
    settings = get_settings()

    if not settings.api_key:
        raise ConfigError(
            "TIGER_API_KEY is not set, so this service will not start.\n"
            "An HTTP endpoint that can place orders must not be reachable "
            "without a key. Add a long random TIGER_API_KEY to .env."
        )

    app = FastAPI(
        title="Tiger Options Backend",
        version="8.0",
        description=(
            "HTTP access to the Tiger options backend. Order placement is "
            "guarded by four locks: the account allowlist, the live opt-in, "
            "DRY_RUN, and this API key. Orders are two-step: preview for a "
            "token, then submit with that token and the exact cash figure."
        ),
        swagger_ui_parameters={"persistAuthorization": True},
    )

    register_middleware(app)
    register_error_handlers(app)
    register_openapi(app)

    app.include_router(health.router)
    app.include_router(market.router)
    app.include_router(contracts.router)
    app.include_router(positions.router)
    app.include_router(orders.router)

    return app


def register_middleware(app: FastAPI) -> None:
    """Attach the API key check in front of every protected route.

    Args:
        app: The application.
    """

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        """Refuse anything without a matching key.

        Compared with secrets.compare_digest so that a wrong key takes the
        same time to reject as a right one.
        """
        if request.url.path in UNPROTECTED_PATHS:
            return await call_next(request)

        supplied_key = request.headers.get(API_KEY_HEADER, "")
        expected_key = get_settings().api_key or ""

        if not supplied_key or not secrets.compare_digest(supplied_key, expected_key):
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "UNAUTHORIZED",
                    "message": (
                        f"This endpoint requires a valid {API_KEY_HEADER} header. "
                        "The key is TIGER_API_KEY from .env."
                    ),
                    "detail": None,
                },
            )

        return await call_next(request)


def register_openapi(app: FastAPI) -> None:
    """Declare the API key in OpenAPI so /docs shows Authorize.

    Enforcement stays in middleware. This only tells Swagger to send
    ``X-API-Key`` on Try it out. /health and the docs themselves stay
    unmarked so they match UNPROTECTED_PATHS.
    """

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": API_KEY_HEADER,
            }
        }
        for path, operations in schema.get("paths", {}).items():
            if path in UNPROTECTED_PATHS:
                continue
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def register_error_handlers(app: FastAPI) -> None:
    """Turn exceptions into the single documented error shape.

    Args:
        app: The application.
    """

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, error: ApiError) -> JSONResponse:
        """Return an error raised deliberately by a route."""
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error_code": error.error_code,
                "message": error.message,
                "detail": error.detail,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """Return a body that failed Pydantic validation, in our error shape."""
        return JSONResponse(
            status_code=422,
            content={
                "error_code": "REQUEST_INVALID",
                "message": "The request body did not match what this endpoint "
                           "expects. See detail for the offending fields.",
                "detail": {"errors": error.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def handle_library_error(_request: Request, error: Exception) -> JSONResponse:
        """Map any library exception onto its documented status and code."""
        api_error = classify_exception(error)
        return JSONResponse(
            status_code=api_error.status_code,
            content={
                "error_code": api_error.error_code,
                "message": api_error.message,
                "detail": api_error.detail,
            },
        )


def run() -> None:
    """Run the service with uvicorn, bound to the configured address.

    Defaults to 127.0.0.1: only this machine can reach it. Exposing an
    order-placing service to a network should be a decision someone makes on
    purpose, by editing API_HOST.
    """
    import uvicorn

    settings = get_settings()

    print("=" * 60)
    print("  TIGER OPTIONS BACKEND - HTTP API")
    print(f"  Account : {settings.masked_account}")
    print(f"  Mode    : {settings.mode}")
    print(f"  Dry run : {'TRUE' if settings.dry_run else 'FALSE'}")
    print(f"  Binding : http://{settings.api_host}:{settings.api_port}")
    if settings.api_host not in ("127.0.0.1", "localhost"):
        print("  *** NOT bound to localhost. This port can place orders. ***")
    print("=" * 60)

    uvicorn.run(
        create_app(),
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
