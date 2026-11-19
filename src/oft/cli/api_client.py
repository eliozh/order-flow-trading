"""Thin HTTP client for the local API (CLI never opens exchange or DuckDB writes)."""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, cast
from urllib.parse import quote

from oft.api.auth import _origin, read_api_token
from oft.config import Settings

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

#: Loopback requests must never be forwarded through an ambient proxy: the
#: default urllib opener honors ``http_proxy``/``ALL_PROXY`` and would send the
#: ``Authorization: Bearer`` header to whatever host they name. An explicit
#: empty ``ProxyHandler`` makes both request paths connect directly.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: Daemon-supplied diagnostics are untrusted text: one bounded line, no controls.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_ERROR_TEXT = 500
#: Catalog codes are uppercase words; anything else is an internal failure.
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: ``new_request_id()`` emits ``REQ-`` plus uppercase hex.
_REFERENCE_ID = re.compile(r"^REQ-[A-Za-z0-9]{1,64}$")


def _safe_error_text(value: str) -> str:
    """Return a control-stripped, length-bounded one-line diagnostic."""
    return _CONTROL_CHARACTERS.sub("", value)[:_MAX_ERROR_TEXT]


def _safe_error_code(value: object) -> str:
    if isinstance(value, str) and _ERROR_CODE.fullmatch(value):
        return value
    return "INTERNAL_ERROR"


def _safe_reference_id(value: object) -> str | None:
    if isinstance(value, str) and _REFERENCE_ID.fullmatch(value):
        return value
    return None


class ApiClientError(Exception):
    """A typed API failure; ``code`` matches ``error-catalog.json``."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        reference_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.reference_id = reference_id


class ApiClient:
    """Minimal JSON client using only the standard library."""

    def __init__(self, base_url: str, token: str | None, *, timeout: float | None = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings, *, timeout: float | None = 2.0) -> ApiClient:
        token = os.environ.get("OFT_API_TOKEN") or read_api_token(settings)
        # ``_origin`` brackets IPv6 literals (``http://[::1]:port``); a bare
        # ``::1`` would make urllib emit an unparseable Host header.
        return cls(_origin(settings.host, settings.port), token, timeout=timeout)

    def _api_client_error(
        self,
        exc: urllib.error.HTTPError | urllib.error.URLError | TimeoutError | OSError,
    ) -> ApiClientError:
        """Map a transport failure to the typed error both request paths raise."""
        if isinstance(exc, urllib.error.HTTPError):
            # The daemon normally sends UTF-8 JSON, but a proxy or crash can
            # return arbitrary bytes -- or fail mid-body. Neither may raise past
            # the CLI or escape the typed-error contract.
            try:
                raw_body = exc.read()
            except (http.client.HTTPException, OSError) as read_exc:
                return ApiClientError(
                    "INTERNAL_ERROR",
                    f"Could not read the daemon error body: {_safe_error_text(str(read_exc))}",
                    status=exc.code,
                )
            body = raw_body.decode("utf-8", errors="replace")
            safe_body = _safe_error_text(body)
            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                return ApiClientError("INTERNAL_ERROR", safe_body, status=exc.code)
            error = envelope.get("error") if isinstance(envelope, dict) else None
            if not isinstance(error, dict):
                return ApiClientError("INTERNAL_ERROR", safe_body, status=exc.code)
            message = error.get("message")
            return ApiClientError(
                _safe_error_code(error.get("code")),
                _safe_error_text(message) if isinstance(message, str) and message else safe_body,
                status=exc.code,
                reference_id=_safe_reference_id(error.get("reference_id")),
            )
        return ApiClientError(
            "DAEMON_NOT_RUNNING",
            f"Local daemon is not reachable at {self._base_url}: {exc}",
        )

    def _malformed_response_error(self, exc: http.client.HTTPException) -> ApiClientError:
        """Map a truncated/malformed HTTP response to a typed internal error."""
        return ApiClientError(
            "INTERNAL_ERROR",
            f"Malformed HTTP response from {self._base_url}: {_safe_error_text(str(exc))}",
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = dict(_JSON_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with _NO_PROXY_OPENER.open(request, timeout=self._timeout) as response:
                return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            raise self._api_client_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise self._api_client_error(exc) from exc
        except http.client.HTTPException as exc:
            raise self._malformed_response_error(exc) from exc

    def create_scan(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/scans", payload, extra_headers=headers)

    def set_starred(self, symbol: str, starred: bool) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/v1/symbols/{symbol}/star",
            {"starred": starred},
            extra_headers={"X-OFT-Surface": "cli"},
        )

    def list_scans(self, limit: int = 20) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/scans?limit={limit}")

    def get_scan(self, scan_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/scans/{quote(scan_id, safe='')}")

    def list_reports(self, limit: int = 20, scan_id: str | None = None) -> dict[str, Any]:
        query = f"?limit={limit}"
        if scan_id:
            query += f"&scan_id={quote(scan_id, safe='')}"
        return self._request("GET", f"/api/v1/reports{query}")

    def get_report(self, report_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/reports/{quote(report_id, safe='')}")

    def export_report(self, report_id: str, *, export_format: str) -> tuple[bytes, dict[str, str]]:
        """Fetch a report export; return the raw body plus lowercase headers."""
        return self._request_bytes(
            "GET",
            f"/api/v1/reports/{quote(report_id, safe='')}/export"
            f"?format={quote(export_format, safe='')}",
        )

    def _request_bytes(self, method: str, path: str) -> tuple[bytes, dict[str, str]]:
        """GET a non-JSON body with the same typed error mapping as ``_request``."""
        headers = {"Accept": "*/*"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(f"{self._base_url}{path}", headers=headers, method=method)
        try:
            with _NO_PROXY_OPENER.open(request, timeout=self._timeout) as response:
                body = response.read()
                response_headers = {name.lower(): value for name, value in response.headers.items()}
                return body, response_headers
        except urllib.error.HTTPError as exc:
            raise self._api_client_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise self._api_client_error(exc) from exc
        except http.client.HTTPException as exc:
            raise self._malformed_response_error(exc) from exc

    def create_backfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/backfill", payload)

    def get_backfill(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/backfill/{quote(run_id, safe='')}")

    def get_latest_backfill(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/backfill/latest")

    # -- Opportunities ---------------------------------------------------

    def list_opportunities(self, *, state: str | None = None, limit: int = 100) -> dict[str, Any]:
        query = f"?limit={limit}"
        if state:
            query += f"&state={quote(state, safe='')}"
        return self._request("GET", f"/api/v1/opportunities{query}")

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/opportunities/{quote(opportunity_id, safe='')}")

    def get_opportunity_timeline(self, opportunity_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/opportunities/{quote(opportunity_id, safe='')}/timeline"
        )

    def get_opportunity_conditions(self, opportunity_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/opportunities/{quote(opportunity_id, safe='')}/conditions"
        )

    def export_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/opportunities/{quote(opportunity_id, safe='')}/export"
        )

    # -- Triggers --------------------------------------------------------

    def list_triggers(
        self,
        *,
        opportunity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = f"?limit={limit}"
        if opportunity_id:
            query += f"&opportunity_id={quote(opportunity_id, safe='')}"
        if status:
            query += f"&status={quote(status, safe='')}"
        return self._request("GET", f"/api/v1/triggers{query}")

    def evaluate_all_triggers(self, *, idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/triggers/evaluate-all", {}, extra_headers=headers)

    def get_trigger(self, trigger_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/triggers/{quote(trigger_id, safe='')}")

    def get_trigger_decision(self, trigger_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/triggers/{quote(trigger_id, safe='')}/decision")

    def recheck_trigger(
        self, trigger_id: str, *, refresh: bool = False, idempotency_key: str
    ) -> dict[str, Any]:
        query = "?refresh=true" if refresh else ""
        return self._request(
            "POST",
            f"/api/v1/triggers/{quote(trigger_id, safe='')}/recheck{query}",
            {},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def evaluate_trigger(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/triggers/evaluate", payload, extra_headers=headers)

    def accept_trigger(self, trigger_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/triggers/{quote(trigger_id, safe='')}/accept",
            {},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def reject_trigger(
        self, trigger_id: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/triggers/{quote(trigger_id, safe='')}/reject",
            payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    # -- Paper and kill switch -------------------------------------------

    def paper_balance(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/paper/account")

    def paper_orders(self, limit: int = 100) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/paper/orders?limit={limit}")

    def paper_fills(self, limit: int = 100) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/paper/fills?limit={limit}")

    def paper_equity(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/paper/equity")

    def kill_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/kill-switch")

    def set_kill_switch(self, *, active: bool, token: str | None = None) -> dict[str, Any]:
        if active:
            return self._request("POST", "/api/v1/kill-switch")
        query = f"?token={quote(token, safe='')}" if token is not None else ""
        return self._request("DELETE", f"/api/v1/kill-switch{query}")

    # -- Positions and the trade journal ---------------------------------

    def list_positions(self, *, state: str | None = None, limit: int = 100) -> dict[str, Any]:
        query = f"?limit={limit}"
        if state:
            query += f"&state={quote(state, safe='')}"
        return self._request("GET", f"/api/v1/positions{query}")

    def get_position(self, position_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/positions/{quote(position_id, safe='')}")

    def position_history(self, position_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/positions/{quote(position_id, safe='')}/history")

    def monitor_position(self, position_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/positions/{quote(position_id, safe='')}/monitor", {})

    def manage_position(
        self,
        position_id: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/positions/{quote(position_id, safe='')}/manage",
            payload or {},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def list_trade_logs(self, *, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
        query = f"?limit={limit}"
        if symbol:
            query += f"&symbol={quote(symbol, safe='')}"
        return self._request("GET", f"/api/v1/journal{query}")

    def get_trade_log(self, trade_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/journal/{quote(trade_id, safe='')}")

    def journal_stats(
        self, *, period: str | None = None, symbol: str | None = None
    ) -> dict[str, Any]:
        query = []
        if period:
            query.append(f"period={quote(period, safe='')}")
        if symbol:
            query.append(f"symbol={quote(symbol, safe='')}")
        suffix = "?" + "&".join(query) if query else ""
        return self._request("GET", f"/api/v1/journal/stats{suffix}")

    def journal_compare(self, trade_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/journal/compare/{quote(trade_id, safe='')}")

    def journal_review(
        self,
        trade_id: str,
        *,
        refresh: bool = False,
        provider: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        query = []
        if refresh:
            query.append("refresh=true")
        if provider:
            query.append(f"provider={quote(provider, safe='')}")
        suffix = "?" + "&".join(query) if query else ""
        return self._request(
            "POST",
            f"/api/v1/journal/{quote(trade_id, safe='')}/review{suffix}",
            {},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def get_journal_review(self, trade_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/journal/{quote(trade_id, safe='')}/review")

    def journal_export(
        self,
        *,
        format: str = "csv",
        period: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        query = [f"format={quote(format, safe='')}"]
        if period:
            query.append(f"period={quote(period, safe='')}")
        if symbol:
            query.append(f"symbol={quote(symbol, safe='')}")
        return self._request("GET", f"/api/v1/journal/export?{'&'.join(query)}")

    # -- Prompts ---------------------------------------------------------

    def list_prompts(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/prompts")

    def get_prompt(self, prompt_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/prompts/{quote(prompt_id, safe='')}")

    def prompt_history(self, prompt_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/prompts/{quote(prompt_id, safe='')}/history")

    def prompt_diff(self, prompt_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/prompts/{quote(prompt_id, safe='')}/diff"
            f"?from_version={from_version}&to_version={to_version}",
        )

    def edit_prompt(
        self, prompt_id: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/prompts/{quote(prompt_id, safe='')}/versions",
            payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def activate_prompt(
        self, prompt_id: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/prompts/{quote(prompt_id, safe='')}/activate",
            payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def restore_prompt(
        self, prompt_id: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/prompts/{quote(prompt_id, safe='')}/restore",
            payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
