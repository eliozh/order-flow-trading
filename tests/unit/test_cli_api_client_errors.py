"""Typed CLI errors for malformed daemon bodies and daemon reference ids."""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
from typing import Any

import pytest

from oft.cli.api_client import ApiClient, ApiClientError


def _client() -> ApiClient:
    return ApiClient("http://127.0.0.1:7611", None)


def _http_error(body: bytes, status: int = 500) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1:7611", status, "boom", {}, io.BytesIO(body))


def test_a_non_utf8_error_body_becomes_a_typed_error() -> None:
    mapped = _client()._api_client_error(_http_error(b"\xff\xfe"))

    assert isinstance(mapped, ApiClientError)
    assert mapped.code == "INTERNAL_ERROR"
    assert mapped.status == 500
    assert isinstance(mapped.message, str)


def test_a_json_non_object_error_body_becomes_a_typed_error() -> None:
    mapped = _client()._api_client_error(_http_error(b'["not", "an", "object"]'))

    assert mapped.code == "INTERNAL_ERROR"
    assert mapped.message == '["not", "an", "object"]'


def test_non_string_error_code_and_message_become_typed_defaults() -> None:
    body = json.dumps({"error": {"code": 7, "message": {"nested": True}, "reference_id": 9}})

    mapped = _client()._api_client_error(_http_error(body.encode("utf-8")))

    assert mapped.code == "INTERNAL_ERROR"
    assert isinstance(mapped.message, str)
    assert mapped.reference_id is None


def test_a_daemon_reference_id_is_carried_on_the_error() -> None:
    body = json.dumps(
        {
            "ok": False,
            "error": {
                "code": "NOT_FOUND",
                "message": "Report RPT-X does not exist.",
                "reference_id": "REQ-ABC123DEF456",
            },
        }
    )

    mapped = _client()._api_client_error(_http_error(body.encode("utf-8"), status=404))

    assert mapped.code == "NOT_FOUND"
    assert mapped.reference_id == "REQ-ABC123DEF456"


@pytest.mark.parametrize("call", ["get_report", "export_report"])
def test_http_exceptions_are_mapped_to_typed_errors(
    monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise http.client.HTTPException("bad status line")

    monkeypatch.setattr("oft.cli.api_client.urllib.request.urlopen", boom)
    client = _client()

    with pytest.raises(ApiClientError) as excinfo:
        if call == "get_report":
            client.get_report("RPT-X")
        else:
            client.export_report("RPT-X", export_format="md")

    assert excinfo.value.code == "INTERNAL_ERROR"
    assert "bad status line" in excinfo.value.message


class _BrokenBody:
    """An error-response body whose ``read`` fails mid-stream."""

    def read(self, *_args: Any, **_kwargs: Any) -> bytes:
        raise http.client.IncompleteRead(b"partial")

    def close(self) -> None:
        return None


def _broken_http_error(status: int = 502) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1:7611", status, "boom", {}, _BrokenBody())


def test_a_failing_error_body_read_becomes_a_typed_error() -> None:
    mapped = _client()._api_client_error(_broken_http_error())

    assert isinstance(mapped, ApiClientError)
    assert mapped.code == "INTERNAL_ERROR"
    assert mapped.status == 502


@pytest.mark.parametrize("call", ["get_report", "export_report"])
def test_a_failing_error_body_read_is_typed_in_both_request_paths(
    monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    def raise_broken(*_args: Any, **_kwargs: Any) -> Any:
        raise _broken_http_error()

    monkeypatch.setattr("oft.cli.api_client.urllib.request.urlopen", raise_broken)
    client = _client()

    with pytest.raises(ApiClientError) as excinfo:
        if call == "get_report":
            client.get_report("RPT-X")
        else:
            client.export_report("RPT-X", export_format="md")

    assert excinfo.value.code == "INTERNAL_ERROR"
    assert "Could not read the daemon error body" in excinfo.value.message


def test_error_message_is_control_stripped_and_bounded() -> None:
    message = "\x1b[31m" + "x" * 700
    body = json.dumps({"error": {"code": "INTERNAL_ERROR", "message": message}})

    mapped = _client()._api_client_error(_http_error(body.encode("utf-8")))

    assert "\x1b" not in mapped.message
    assert len(mapped.message) <= 500


def test_a_control_character_reference_id_is_dropped() -> None:
    body = json.dumps(
        {"error": {"code": "NOT_FOUND", "message": "x", "reference_id": "REQ-\x1b[31mBAD"}}
    )

    mapped = _client()._api_client_error(_http_error(body.encode("utf-8"), status=404))

    assert mapped.reference_id is None


def test_an_invalid_error_code_becomes_internal_error() -> None:
    body = json.dumps({"error": {"code": "not a catalog code", "message": "x"}})

    mapped = _client()._api_client_error(_http_error(body.encode("utf-8")))

    assert mapped.code == "INTERNAL_ERROR"
