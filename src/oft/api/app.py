"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from oft import __version__
from oft.adapters.fixture_market_data import FixtureDataError
from oft.api.auth import (
    CSRF_COOKIE,
    allowed_origins,
    csrf_matches,
    ensure_api_token,
    extract_bearer,
    is_browser_request,
    new_csrf_token,
    token_matches,
)
from oft.api.envelope import new_request_id
from oft.api.errors import ApiError, error_response
from oft.api.routes.scans import router as scans_router
from oft.api.routes.system import router as system_router
from oft.bootstrap import build_container
from oft.config import ConfigError, Settings, load_settings
from oft.logging import configure_logging

logger = structlog.get_logger(__name__)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_COOKIE_PATHS = frozenset({"/health", "/ready", "/api/v1/status"})
_HTTP_ERRORS = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _harden(response: Any) -> Any:
    """Apply the ADR-0010 response hardening headers to every response."""
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


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

    @app.middleware("http")
    async def _security(request: Request, call_next: Any) -> Any:
        path = request.url.path
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
            if presented is None:
                return _harden(
                    error_response("AUTH_REQUIRED", headers={"WWW-Authenticate": "Bearer"})
                )
            if not token_matches(app.state.api_token, presented):
                return _harden(
                    error_response("AUTH_INVALID", headers={"WWW-Authenticate": "Bearer"})
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
