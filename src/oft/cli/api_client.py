"""Thin HTTP client for the local API (CLI never opens exchange or DuckDB writes)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, cast
from urllib.parse import quote

from oft.api.auth import _origin, read_api_token
from oft.config import Settings

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


class ApiClientError(Exception):
    """A typed API failure; ``code`` matches ``error-catalog.json``."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                raise ApiClientError("INTERNAL_ERROR", body, status=exc.code) from exc
            error = envelope.get("error") or {}
            raise ApiClientError(
                error.get("code", "INTERNAL_ERROR"),
                error.get("message", body),
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiClientError(
                "DAEMON_NOT_RUNNING",
                f"Local daemon is not reachable at {self._base_url}: {exc}",
            ) from exc

    def create_scan(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/scans", payload, extra_headers=headers)

    def set_starred(self, symbol: str, starred: bool) -> dict[str, Any]:
        return self._request("PATCH", f"/api/v1/symbols/{symbol}/star", {"starred": starred})

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

    def monitor_position(self, position_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/positions/{quote(position_id, safe='')}/monitor", {})

    def list_trade_logs(self, *, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
        query = f"?limit={limit}"
        if symbol:
            query += f"&symbol={quote(symbol, safe='')}"
        return self._request("GET", f"/api/v1/journal{query}")

    def get_trade_log(self, trade_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/journal/{quote(trade_id, safe='')}")

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
