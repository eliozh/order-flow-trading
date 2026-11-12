"""Recorded LLM responses keyed by ``(role, input_hash)`` (ADR-0011).

Replay never invents a provider output. A recording is a small JSON document
under ``<recordings_dir>/<ROLE>/<input_hash>.json`` holding the exact raw text a
provider returned for that request identity. The document carries a
``content_hash`` so a tampered recording fails closed, and it is written with
secrets redacted.

Two sources implement the provider port:

* :class:`RecordedLLMSource` reads only committed recordings; a missing one
  raises :class:`LLMRecordingError` with the role and input hash and a hint to
  pass ``--live-llm``.
* :class:`RecordingLLMSource` wraps a real (or fixture) provider, calls it, and
  persists the redacted result. It is the opt-in ``--live-llm``/capture path.

Neither source synthesises text: replay output is always either a recording or
an explicit provider call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import (
    CostEstimate,
    LLMAdapterError,
    LLMPort,
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    redact,
)
from oft.replay.harness import write_atomic

RECORDING_FORMAT_VERSION = "1.0.0"
RECORDED_PRICE_VERSION = "recorded/v1"
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class LLMRecordingError(LLMAdapterError):
    """No recording exists for a request; replay refuses to invent one."""

    code = "LLM_RECORDING_MISSING"
    retryable = False

    def __init__(self, role: str, input_hash: str, recordings_dir: Path) -> None:
        self.role = role
        self.input_hash = input_hash
        self.recordings_dir = recordings_dir
        super().__init__(
            f"no recorded LLM output for role={role} input_hash={input_hash}; "
            f"record one under {recordings_dir} or pass --live-llm"
        )


class LLMRecordingInvalid(LLMAdapterError):
    """A recording exists but is unreadable, tampered, or for another request."""

    code = "LLM_RECORDING_INVALID"
    retryable = False


def _segment(value: str, *, field: str) -> str:
    """Reject a path-like key segment so recordings cannot escape the root."""
    if _SEGMENT_PATTERN.fullmatch(value) is None:
        raise LLMRecordingInvalid(f"{field} is not a safe recording key: {value!r}")
    return value


def _recording_path(root: Path, role: str, input_hash: str) -> Path:
    role_segment = _segment(role, field="role")
    hash_segment = _segment(input_hash, field="input_hash")
    path = (root / role_segment / f"{hash_segment}.json").resolve()
    resolved_root = root.resolve()
    if not path.is_relative_to(resolved_root):
        raise LLMRecordingInvalid("recording path escaped the recordings directory")
    return path


def _content_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


class RecordedLLMSource:
    """Deterministic replay provider backed only by committed recordings.

    ``provider`` is the config slot (``stub``) so replay passes the existing
    ``provider_configured`` gate without an API key. The returned result keeps
    the *recorded* provider/model so decision provenance stays honest.
    """

    provider = "stub"

    def __init__(self, *, recordings_dir: Path) -> None:
        self._root = Path(recordings_dir).expanduser().resolve()

    @property
    def recordings_dir(self) -> Path:
        return self._root

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="stub",
            strict_schema=True,
            max_output_tokens=4096,
            notes=f"recorded replay source under {self._root.name}",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version=RECORDED_PRICE_VERSION, unpriced=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        path = _recording_path(self._root, request.role.value, request.input_hash)
        if not path.is_file():
            raise LLMRecordingError(request.role.value, request.input_hash, self._root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMRecordingInvalid(f"recorded LLM output is unreadable: {path.name}") from exc
        if not isinstance(payload, dict):
            raise LLMRecordingInvalid(f"recorded LLM output is not an object: {path.name}")
        if payload.get("role") != request.role.value:
            raise LLMRecordingInvalid("recorded LLM output is for a different role")
        if payload.get("input_hash") != request.input_hash:
            raise LLMRecordingInvalid("recorded LLM output is for a different input hash")
        raw_text = payload.get("raw_text")
        if not isinstance(raw_text, str):
            raise LLMRecordingInvalid("recorded LLM output has no raw_text")
        if payload.get("content_hash") != _content_hash(raw_text):
            raise LLMRecordingInvalid(
                "recorded LLM output was tampered with (content hash mismatch)"
            )
        return LLMResult(
            provider=str(payload.get("provider") or "recorded"),
            model=str(payload.get("model") or "recorded"),
            raw_text=raw_text,
            parsed=None,
            latency_ms=0,
            request_id=request.request_id or f"replay-{request.input_hash[:12]}",
            refusal=bool(payload.get("refusal", False)),
            truncated=bool(payload.get("truncated", False)),
            capabilities=self.capabilities(),
        )


class RecordingLLMSource:
    """Wrap a provider, call it once, and persist a redacted recording.

    This is the explicit ``--live-llm``/capture path. The inner provider may be
    the offline fixture stub; recording it is what makes later replays read-only.
    """

    def __init__(
        self,
        *,
        inner: LLMPort,
        recordings_dir: Path,
        clock: ClockPort,
        secrets: Iterable[str] = (),
    ) -> None:
        self._inner = inner
        self._root = Path(recordings_dir).expanduser().resolve()
        self._clock = clock
        self._secrets = tuple(value for value in secrets if value)

    @property
    def provider(self) -> str:
        return str(getattr(self._inner, "provider", "") or "stub")

    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities()

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return self._inner.estimate_cost(request, model)

    def complete(self, request: LLMRequest) -> LLMResult:
        result = self._inner.complete(request)
        self.record(request, result)
        return result

    def record(self, request: LLMRequest, result: LLMResult) -> Path:
        redacted_text = redact(result.raw_text, self._secrets)
        path = _recording_path(self._root, request.role.value, request.input_hash)
        payload = {
            "recording_format_version": RECORDING_FORMAT_VERSION,
            "role": request.role.value,
            "input_hash": request.input_hash,
            "provider": result.provider,
            "model": result.model,
            "raw_text": redacted_text,
            "content_hash": _content_hash(redacted_text),
            "redacted": redacted_text != result.raw_text,
            "refusal": result.refusal,
            "truncated": result.truncated,
            "recorded_at": utc_timestamp(self._clock.now()),
        }
        write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path


__all__ = [
    "LLMRecordingError",
    "LLMRecordingInvalid",
    "RECORDING_FORMAT_VERSION",
    "RecordedLLMSource",
    "RecordingLLMSource",
]
