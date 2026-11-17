"""Scripted Telegram Bot API transport plus secret-backend helpers.

The transport is an ``httpx.BaseTransport``: the production adapter performs a
real Bot API request shape, but nothing ever leaves the process. Every request
is recorded so a test can assert the path, JSON body, and the absence of a
``parse_mode`` field.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from oft.application.secrets_service import SecretBackend

BOT_API_HOST = "https://api.telegram.org"


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    json: dict[str, Any]
    host: str


def make_update(
    update_id: int,
    *,
    chat_id: int = 1001,
    user_id: int = 2002,
    text: str = "/status",
    message_id: int | None = None,
) -> dict[str, Any]:
    """One Bot API ``getUpdates`` result item."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id if message_id is not None else update_id + 100,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "operator"},
            "text": text,
        },
    }


class ScriptedBotApiTransport(httpx.BaseTransport):
    """Map Bot API method paths to deterministic JSON and record every call."""

    def __init__(
        self,
        *,
        updates: list[dict[str, Any]] | None = None,
        sent_message_id_start: int = 500,
    ) -> None:
        self.requests: list[RecordedRequest] = []
        self.updates = list(updates or [])
        self.sent: list[dict[str, Any]] = []
        self._next_message_id = sent_message_id_start
        self._errors: dict[str, list[tuple[int, int | None]]] = {}
        self._lock = threading.Lock()

    def script_error(self, method: str, error_code: int, *, retry_after: int | None = None) -> None:
        """Make the next ``method`` call fail with one Bot API error code."""
        with self._lock:
            self._errors.setdefault(method, []).append((error_code, retry_after))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        path = request.url.path
        method = path.rsplit("/", 1)[-1]
        with self._lock:
            self.requests.append(
                RecordedRequest(
                    method=request.method,
                    path=path,
                    json=body,
                    host=request.url.host,
                )
            )
            pending = self._errors.get(method)
            if pending:
                error_code, retry_after = pending.pop(0)
                error_body: dict[str, Any] = {
                    "ok": False,
                    "error_code": error_code,
                    "description": "scripted failure",
                }
                if retry_after is not None:
                    error_body["parameters"] = {"retry_after": retry_after}
                return httpx.Response(error_code, json=error_body)
            if method == "getMe":
                return httpx.Response(
                    200, json={"ok": True, "result": {"id": 42, "username": "oft_bot"}}
                )
            if method == "getUpdates":
                offset = body.get("offset")
                result = [
                    update
                    for update in self.updates
                    if offset is None or int(update["update_id"]) >= int(offset)
                ]
                return httpx.Response(200, json={"ok": True, "result": result})
            if method == "sendMessage":
                self._next_message_id += 1
                message_id = self._next_message_id
                self.sent.append(
                    {
                        "chat_id": body.get("chat_id"),
                        "text": body.get("text"),
                        "reply_to_message_id": body.get("reply_to_message_id"),
                        "message_id": message_id,
                        "json": dict(body),
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "message_id": message_id,
                            "chat": {"id": body.get("chat_id")},
                            "text": body.get("text"),
                        },
                    },
                )
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "unknown method"}
            )


class BlockingBotApiTransport(httpx.BaseTransport):
    """Block every request until the test releases it (scheduler independence)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=10)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


class MemorySecretBackend(SecretBackend):
    """An in-process backend so tests never touch the OS keyring or disk."""

    kind = "encrypted_file"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)
