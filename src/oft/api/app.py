"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from oft import __version__
from oft.api.envelope import error_envelope, new_request_id
from oft.api.routes.system import router as system_router
from oft.config import Settings, load_settings
from oft.logging import configure_logging

logger = structlog.get_logger(__name__)

_HTTP_ERRORS = {
    404: ("NOT_FOUND", "VALIDATION"),
    405: ("METHOD_NOT_ALLOWED", "VALIDATION"),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the local API application.

    Run with ``uvicorn oft.api.app:create_app --factory``.
    """
    resolved = settings or load_settings()
    configure_logging(resolved)

    app = FastAPI(
        title="Order Flow Trading",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.include_router(system_router)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, category = _HTTP_ERRORS.get(exc.status_code, ("INTERNAL_ERROR", "SYSTEM"))
        request_id = new_request_id()
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                code=code,
                message=str(exc.detail),
                http_status=exc.status_code,
                category=category,
                retryable=False,
                reference_id=request_id,
                request_id=request_id,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
        request_id = new_request_id()
        logger.exception("unhandled_api_error", request_id=request_id, error=str(exc))
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected internal error occurred.",
                http_status=500,
                category="SYSTEM",
                retryable=True,
                reference_id=request_id,
                request_id=request_id,
            ),
        )

    dist_dir = _web_dist()
    if dist_dir is not None:
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="web")

    return app


def _web_dist() -> Path | None:
    candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
    return candidate if candidate.is_dir() else None
