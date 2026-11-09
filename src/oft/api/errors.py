"""Stable API error codes and envelope responses (ADR-0003)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse

from oft.api.envelope import error_envelope, new_request_id


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    category: str
    retryable: bool
    message: str


ERROR_CATALOG: dict[str, ErrorSpec] = {
    "AUTH_REQUIRED": ErrorSpec(401, "AUTH", False, "No local API credential was supplied."),
    "AUTH_INVALID": ErrorSpec(
        401, "AUTH", False, "The local API credential is invalid or expired."
    ),
    "CSRF_INVALID": ErrorSpec(
        403, "AUTH", False, "The CSRF token is missing or does not match the session."
    ),
    "ORIGIN_FORBIDDEN": ErrorSpec(
        403,
        "AUTH",
        False,
        "The request Origin/Sec-Fetch-Site does not match the served origin.",
    ),
    "HOST_FORBIDDEN": ErrorSpec(
        403,
        "AUTH",
        False,
        "The Host header is not an allowed loopback host.",
    ),
    "NOT_FOUND": ErrorSpec(
        404, "VALIDATION", False, "The requested object or route does not exist."
    ),
    "METHOD_NOT_ALLOWED": ErrorSpec(
        405, "VALIDATION", False, "The HTTP method is not valid for this route."
    ),
    "VALIDATION_FAILED": ErrorSpec(
        422, "VALIDATION", False, "One or more request fields failed validation."
    ),
    "CONFLICT": ErrorSpec(409, "CONFLICT", False, "The request conflicts with the current state."),
    "IDEMPOTENCY_KEY_REQUIRED": ErrorSpec(
        400,
        "CONFLICT",
        False,
        "A state-changing request was sent without an Idempotency-Key.",
    ),
    "IDEMPOTENCY_CONFLICT": ErrorSpec(
        409,
        "CONFLICT",
        False,
        "The same idempotency key was used with a different request payload.",
    ),
    "METRICS_INCOMPLETE": ErrorSpec(
        409,
        "DATA",
        True,
        "One or more required metrics could not be computed.",
    ),
    "CONFIG_INVALID": ErrorSpec(
        500, "SYSTEM", False, "The configuration is missing, unreadable, or inconsistent."
    ),
    "DATA_UNAVAILABLE": ErrorSpec(
        503, "DATA", True, "Required data does not exist or cannot be fetched."
    ),
    "STORAGE_UNAVAILABLE": ErrorSpec(
        503, "STORAGE", True, "SQLite or the Parquet catalog is unavailable."
    ),
    "INTERNAL_ERROR": ErrorSpec(500, "SYSTEM", True, "An unexpected internal error occurred."),
    "CLOCK_DRIFT": ErrorSpec(
        409,
        "SYSTEM",
        True,
        "The local clock differs from Binance server time beyond the allowed threshold.",
    ),
    "DAEMON_NOT_RUNNING": ErrorSpec(
        503,
        "SYSTEM",
        True,
        "The daemon is not running, so the mutating command cannot execute.",
    ),
    "SCAN_ALREADY_RUNNING": ErrorSpec(
        409,
        "CONFLICT",
        True,
        "A scan is already running.",
    ),
    "BACKFILL_CHECKSUM_MISMATCH": ErrorSpec(
        502,
        "DATA",
        True,
        "A downloaded archive file failed SHA256 verification.",
    ),
    "BACKFILL_ALREADY_RUNNING": ErrorSpec(
        409,
        "CONFLICT",
        True,
        "A backfill is already running.",
    ),
    "LLM_PROVIDER_NOT_CONFIGURED": ErrorSpec(
        412, "LLM", False, "No LLM provider/key is configured for this role."
    ),
    "LLM_AUTH_FAILED": ErrorSpec(502, "LLM", False, "The LLM provider rejected the API key."),
    "LLM_MODEL_NOT_CONFIGURED": ErrorSpec(
        412, "LLM", False, "No model is configured for this LLM role."
    ),
    "LLM_TIMEOUT": ErrorSpec(504, "LLM", True, "The LLM call exceeded its timeout."),
    "LLM_RATE_LIMITED": ErrorSpec(429, "LLM", True, "The LLM provider rate-limited the request."),
    "UPSTREAM_UNAVAILABLE": ErrorSpec(
        502, "UPSTREAM", True, "An upstream service is unreachable or returned an invalid response."
    ),
    "LLM_SCHEMA_UNSUPPORTED": ErrorSpec(
        422,
        "LLM",
        False,
        "The LLM provider cannot enforce the requested structured output schema.",
    ),
    "LLM_SCHEMA_INVALID": ErrorSpec(
        422,
        "LLM",
        False,
        "The model output failed schema or semantic validation after the repair attempt.",
    ),
    "LLM_OUTPUT_TRUNCATED": ErrorSpec(
        422, "LLM", False, "The model output was truncated (incomplete status)."
    ),
    "LLM_REFUSED": ErrorSpec(422, "LLM", False, "The model refused the request."),
    "PROMPT_VALIDATION_FAILED": ErrorSpec(
        422,
        "LLM",
        False,
        "A prompt template is missing a required variable or has invalid syntax.",
    ),
    "PROMPT_VERSION_MISSING": ErrorSpec(
        409,
        "LLM",
        False,
        "A decision references a prompt version that no longer exists or has no "
        "pinned system version.",
    ),
    "KILL_SWITCH_ACTIVE": ErrorSpec(
        423, "RISK", False, "The kill switch blocks new orders and LLM-driven execution."
    ),
    "RISK_BLOCKED": ErrorSpec(422, "RISK", False, "The deterministic risk gate blocked the order."),
    "RISK_DATA_INCOMPLETE": ErrorSpec(
        409,
        "RISK",
        True,
        "A required risk input (mark, bracket, balance, position) is missing or stale.",
    ),
    "ORDER_REJECTED": ErrorSpec(
        422,
        "EXECUTION",
        False,
        "Binance rejected the order (filters, margin, permissions, or state).",
    ),
    "ORDER_DUPLICATE": ErrorSpec(
        409,
        "EXECUTION",
        False,
        "An order with this client order id or idempotency key already exists.",
    ),
    "ORDER_NOT_FOUND": ErrorSpec(
        404, "EXECUTION", False, "The order is not known locally or at the exchange."
    ),
}


class ApiError(Exception):
    """Raise inside a route to emit a catalog error envelope."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        spec = ERROR_CATALOG[code]
        super().__init__(message or spec.message)
        self.code = code
        self.message = message or spec.message
        self.details = details or []


def error_response(
    code: str,
    *,
    message: str | None = None,
    details: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    spec = ERROR_CATALOG[code]
    resolved_request_id = request_id or new_request_id()
    return JSONResponse(
        status_code=spec.http_status,
        content=error_envelope(
            code=code,
            message=message or spec.message,
            http_status=spec.http_status,
            category=spec.category,
            retryable=spec.retryable,
            reference_id=resolved_request_id,
            request_id=resolved_request_id,
            details=details,
        ),
        headers=headers,
    )
