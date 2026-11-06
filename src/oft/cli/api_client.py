"""Thin HTTP client for the local API (CLI never opens exchange or DuckDB writes)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, cast

from oft.api.auth import read_api_token
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

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 2.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings, *, timeout: float = 2.0) -> ApiClient:
        token = os.environ.get("OFT_API_TOKEN") or read_api_token(settings)
        return cls(f"http://{settings.host}:{settings.port}", token, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = dict(_JSON_HEADERS)
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

    def create_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/scans", payload)

    def get_scan(self, scan_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/scans/{scan_id}")
