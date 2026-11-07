"""FastAPI application factory."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from oft import __version__
from oft.adapters.fixture_market_data import FixtureDataError
from oft.api.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    SessionStore,
    allowed_origins,
    csrf_matches,
    ensure_api_token,
    extract_bearer,
    is_browser_request,
    new_csrf_token,
    token_matches,
    write_browser_bootstrap,
)
from oft.api.envelope import new_request_id
from oft.api.errors import ApiError, error_response
from oft.api.routes.data import router as data_router
from oft.api.routes.reports import router as reports_router
from oft.api.routes.scans import router as scans_router
from oft.api.routes.symbols import router as symbols_router
from oft.api.routes.system import router as system_router
from oft.api.routes.watchlist import router as watchlist_router
from oft.bootstrap import build_container
from oft.config import ConfigError, Settings, load_settings
from oft.logging import configure_logging
from oft.ports.store import StoreUnavailableError

logger = structlog.get_logger(__name__)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_COOKIE_PATHS = frozenset({"/health", "/ready", "/api/v1/status"})
_SPA_ENTRY_PATHS = frozenset(
    {"/", "/system", "/system/", "/watchlist", "/watchlist/search", "/scans", "/reports"}
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TEST_CLIENT_HOST = "testserver"
_HTTP_ERRORS = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_AUTH_PROMPT_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    "<title>Order Flow Trading — authentication required</title></head>"
    "<body><main><h1>Authentication required</h1>"
    "<p>Run <code>oft open</code> in your terminal to authenticate this browser.</p>"
    "</main></body></html>"
)


_BOOTSTRAP_READY_MESSAGE = "bootstrap ready — run `oft open`"


def _harden(response: Any) -> Any:
    """Apply the ADR-0010 response hardening headers to every response."""
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def host_is_allowed(request: Request, settings: Settings) -> bool:
    """Reject DNS-rebinding Host headers before any auth or routing work."""
    raw_host = request.headers.get("host")
    if not raw_host:
        return False
    try:
        parsed = urlsplit(f"//{raw_host}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        # e.g. ``127.0.0.1:notaport`` or a malformed IPv6 literal.
        return False
    if host is None:
        return False
    # The in-process TestClient sends ``testserver``; only exempt it under test.
    if settings.env == "test" and host == _TEST_CLIENT_HOST:
        return True
    # Allow loopback names plus the explicitly configured bind host; the latter
    # makes `--allow-non-loopback` usable without opening the door to arbitrary
    # DNS-rebinding hosts.
    if host not in _LOOPBACK_HOSTS and host != settings.host:
        return False
    if port is None:
        return settings.port == 80
    return port == settings.port


def _is_spa_entry(path: str) -> bool:
    """Client-rendered entry routes that may establish a browser session."""
    return (
        path in _SPA_ENTRY_PATHS
        or path.startswith("/watchlist/")
        or path.startswith("/scans/")
        or path.startswith("/reports/")
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the local API application.

    Run with ``uvicorn oft.api.app:create_app --factory``.
    """
    resolved = settings or load_settings()
    configure_logging(resolved)
    container = build_container(resolved, initialize=True)

    app = FastAPI(
        title="Order Flow Trading",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.state.container = container
    app.state.api_token = ensure_api_token(resolved)
    app.state.scan_lock = container.scan_lock
    app.state.sessions = SessionStore()
    app.state.browser_bootstrap = write_browser_bootstrap(resolved)
    logger.info("browser_bootstrap_ready", detail=_BOOTSTRAP_READY_MESSAGE)

    @app.middleware("http")
    async def _security(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if not host_is_allowed(request, resolved):
            return _harden(error_response("HOST_FORBIDDEN"))

        if request.method == "GET" and path == "/":
            presented_bootstrap = request.query_params.get("bootstrap")
            expected = app.state.browser_bootstrap
            if (
                presented_bootstrap is not None
                and expected is not None
                and hmac.compare_digest(
                    expected.encode("utf-8"), presented_bootstrap.encode("utf-8")
                )
            ):
                # Single-use bootstrap: rotate immediately after minting a session.
                app.state.browser_bootstrap = write_browser_bootstrap(resolved)
                logger.info("browser_bootstrap_ready", detail=_BOOTSTRAP_READY_MESSAGE)
                response = RedirectResponse("/", status_code=302)
                response.set_cookie(
                    SESSION_COOKIE,
                    app.state.sessions.create(),
                    httponly=True,
                    samesite="strict",
                    secure=request.url.scheme == "https",
                    path="/",
                    max_age=SESSION_TTL_SECONDS,
                )
                return _harden(response)

        session_valid = app.state.sessions.is_valid(request.cookies.get(SESSION_COOKIE))
        if request.method == "GET" and _is_spa_entry(path) and not session_valid:
            return _harden(HTMLResponse(_AUTH_PROMPT_HTML, status_code=401))

        if path.startswith("/api/v1/"):
            if request.method in _MUTATING_METHODS:
                origin = request.headers.get("origin")
                if origin is not None and origin not in allowed_origins(resolved):
                    return _harden(error_response("ORIGIN_FORBIDDEN"))
                fetch_site = request.headers.get("sec-fetch-site")
                if fetch_site in {"cross-site", "same-site"}:
                    return _harden(error_response("ORIGIN_FORBIDDEN"))
                if is_browser_request(request) and not csrf_matches(request):
                    return _harden(error_response("CSRF_INVALID"))

            presented = extract_bearer(request.headers.get("authorization"))
            if presented is not None:
                if not token_matches(app.state.api_token, presented):
                    return _harden(
                        error_response("AUTH_INVALID", headers={"WWW-Authenticate": "Bearer"})
                    )
            elif not session_valid:
                return _harden(
                    error_response("AUTH_REQUIRED", headers={"WWW-Authenticate": "Bearer"})
                )

        response = await call_next(request)
        if request.method == "GET" and path in _COOKIE_PATHS and CSRF_COOKIE not in request.cookies:
            response.set_cookie(
                CSRF_COOKIE,
                new_csrf_token(),
                httponly=False,
                samesite="strict",
                secure=False,
                path="/",
            )
        return _harden(response)

    app.include_router(system_router)
    app.include_router(scans_router)
    app.include_router(reports_router)
    app.include_router(symbols_router)
    app.include_router(watchlist_router)
    app.include_router(data_router)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, message=exc.message, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(
                    str(part) for part in error.get("loc", ()) if part not in {"body", "query"}
                )
                or None,
                "code": error.get("type"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        return error_response("VALIDATION_FAILED", details=details)

    @app.exception_handler(FixtureDataError)
    async def _fixture_error(_request: Request, exc: FixtureDataError) -> JSONResponse:
        return error_response(exc.code, message=str(exc))

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        return error_response(exc.code, message=exc.message)

    @app.exception_handler(StoreUnavailableError)
    async def _store_error(_request: Request, exc: StoreUnavailableError) -> JSONResponse:
        # Keep raw sqlite/OS text out of the client; it goes to the daemon log only.
        logger.warning("store_unavailable", error=exc.message)
        return error_response(exc.code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_ERRORS.get(exc.status_code, "INTERNAL_ERROR")
        return error_response(code, message=str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
        request_id = new_request_id()
        logger.exception("unhandled_api_error", request_id=request_id, error=str(exc))
        return error_response("INTERNAL_ERROR", request_id=request_id)

    dist_dir = _web_dist()
    if dist_dir is not None:
        index_file = dist_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> Any:
            if full_path.startswith("api/") or full_path in {"health", "ready"}:
                raise ApiError("NOT_FOUND")
            candidate = (dist_dir / full_path).resolve()
            if full_path and candidate.is_file() and dist_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            # An asset miss (a path with a file suffix) must not fall through to
            # the SPA HTML; only extensionless routes are client-rendered.
            if Path(full_path).suffix:
                raise ApiError("NOT_FOUND")
            return FileResponse(index_file)

    return app


def _web_dist() -> Path | None:
    candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
    return candidate if candidate.is_dir() else None
