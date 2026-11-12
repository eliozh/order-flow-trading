"""Recorded LLM replay: keyed by role/input_hash, redacted, never synthesised."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.clock import FixedClock
from oft.ports.llm import (
    CostEstimate,
    LLMRequest,
    LLMResult,
    LLMRole,
    ProviderCapabilities,
)
from oft.replay.llm_source import (
    LLMRecordingError,
    LLMRecordingInvalid,
    RecordedLLMSource,
    RecordingLLMSource,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
LLM_FIXTURES = ROOT / "tests" / "fixtures" / "llm"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
INPUT_HASH = "a" * 64


def _request(*, role: LLMRole = LLMRole.ENTRY_RECHECK, input_hash: str = INPUT_HASH) -> LLMRequest:
    return LLMRequest(
        role=role,
        model="fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json="{}",
        input_hash=input_hash,
        prompt_id="entry_recheck",
        prompt_version=1,
        prompt_hash="b" * 64,
        request_id="REQ-TEST",
    )


def _stub() -> LLMStub:
    return LLMStub(fixtures_dir=LLM_FIXTURES)


class _FakeProvider:
    provider = "openai"

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(
            provider="openai",
            model="fake-model",
            raw_text=self.raw_text,
            request_id="REQ-FAKE",
            capabilities=self.capabilities(),
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="fake/v1", unpriced=True)


def test_a_recording_round_trips_by_role_and_input_hash(tmp_path: Path) -> None:
    source = RecordingLLMSource(inner=_stub(), recordings_dir=tmp_path, clock=FixedClock(NOW))

    captured = source.complete(_request())

    recording = tmp_path / "ENTRY_RECHECK" / f"{INPUT_HASH}.json"
    payload = json.loads(recording.read_text(encoding="utf-8"))
    assert payload["role"] == "ENTRY_RECHECK"
    assert payload["input_hash"] == INPUT_HASH
    assert payload["provider"] == "stub"
    assert payload["model"] == "fixture"
    assert payload["redacted"] is False
    assert payload["content_hash"] == hashlib.sha256(payload["raw_text"].encode()).hexdigest()

    replayed = RecordedLLMSource(recordings_dir=tmp_path).complete(_request())

    assert replayed.raw_text == captured.raw_text
    assert replayed.provider == "stub"
    assert replayed.model == "fixture"


def test_a_missing_recording_fails_with_a_clear_error(tmp_path: Path) -> None:
    source = RecordedLLMSource(recordings_dir=tmp_path)

    with pytest.raises(LLMRecordingError) as raised:
        source.complete(_request())

    message = str(raised.value)
    assert "ENTRY_RECHECK" in message
    assert INPUT_HASH in message
    assert "--live-llm" in message
    assert not list(tmp_path.rglob("*.json")), "replay must never create an output"


def test_a_recording_is_redacted_and_checksummed(tmp_path: Path) -> None:
    secret = "sk-live-super-secret-value"
    source = RecordingLLMSource(
        inner=_FakeProvider('{"note": "sk-live-super-secret-value"}'),
        recordings_dir=tmp_path,
        clock=FixedClock(NOW),
        secrets=(secret,),
    )

    source.complete(_request())

    text = (tmp_path / "ENTRY_RECHECK" / f"{INPUT_HASH}.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert secret not in text
    assert payload["redacted"] is True
    assert payload["provider"] == "openai"
    assert payload["content_hash"] == hashlib.sha256(payload["raw_text"].encode()).hexdigest()


def test_a_tampered_recording_fails_closed(tmp_path: Path) -> None:
    source = RecordingLLMSource(inner=_stub(), recordings_dir=tmp_path, clock=FixedClock(NOW))
    source.complete(_request())
    path = tmp_path / "ENTRY_RECHECK" / f"{INPUT_HASH}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["raw_text"] = payload["raw_text"] + "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LLMRecordingInvalid, match="tampered|content hash"):
        RecordedLLMSource(recordings_dir=tmp_path).complete(_request())


def test_a_path_like_input_hash_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LLMRecordingInvalid):
        RecordedLLMSource(recordings_dir=tmp_path).complete(_request(input_hash="../escape"))


def _write_recording(root: Path, payload: object) -> Path:
    directory = root / "ENTRY_RECHECK"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{INPUT_HASH}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "payload",
    [
        [],  # not a JSON object
        {  # recorded for a different role
            "role": "ANALYSIS",
            "input_hash": INPUT_HASH,
            "raw_text": "x",
            "content_hash": hashlib.sha256(b"x").hexdigest(),
        },
        {  # recorded for a different input hash
            "role": "ENTRY_RECHECK",
            "input_hash": "b" * 64,
            "raw_text": "x",
            "content_hash": hashlib.sha256(b"x").hexdigest(),
        },
        {"role": "ENTRY_RECHECK", "input_hash": INPUT_HASH},  # no raw_text
    ],
)
def test_a_malformed_recording_fails_closed(tmp_path: Path, payload: object) -> None:
    _write_recording(tmp_path, payload)

    with pytest.raises(LLMRecordingInvalid):
        RecordedLLMSource(recordings_dir=tmp_path).complete(_request())


def test_an_unreadable_recording_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "ENTRY_RECHECK"
    directory.mkdir(parents=True)
    (directory / f"{INPUT_HASH}.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(LLMRecordingInvalid, match="unreadable"):
        RecordedLLMSource(recordings_dir=tmp_path).complete(_request())


def test_a_symlinked_recording_file_outside_the_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "role": "ENTRY_RECHECK",
                "input_hash": INPUT_HASH,
                "raw_text": "x",
                "content_hash": hashlib.sha256(b"x").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "root"
    directory = root / "ENTRY_RECHECK"
    directory.mkdir(parents=True)
    (directory / f"{INPUT_HASH}.json").symlink_to(outside)

    with pytest.raises(LLMRecordingInvalid, match="escaped"):
        RecordedLLMSource(recordings_dir=root).complete(_request())


def test_a_symlinked_role_directory_outside_the_root_is_rejected(tmp_path: Path) -> None:
    outside_role = tmp_path / "outside-role"
    outside_role.mkdir()
    (outside_role / f"{INPUT_HASH}.json").write_text(
        json.dumps(
            {
                "role": "ENTRY_RECHECK",
                "input_hash": INPUT_HASH,
                "raw_text": "x",
                "content_hash": hashlib.sha256(b"x").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "ENTRY_RECHECK").symlink_to(outside_role, target_is_directory=True)

    with pytest.raises(LLMRecordingInvalid, match="escaped"):
        RecordedLLMSource(recordings_dir=root).complete(_request())


def test_the_recorded_source_is_explicitly_unpriced(tmp_path: Path) -> None:
    source = RecordedLLMSource(recordings_dir=tmp_path)

    estimate = source.estimate_cost(_request(), "fixture")

    assert estimate.unpriced is True
    assert estimate.price_version == "recorded/v1"
    assert source.capabilities().provider == "stub"
