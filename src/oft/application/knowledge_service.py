"""Knowledge lifecycle: build, review/approve, activate, and load artifacts.

The service owns the private ``$OFT_DATA/knowledge`` tree. It never reads raw
book text: ``build`` ingests markdown skill outputs (``book-to-skill`` shape),
compiles a budgeted playbook, and writes ``playbook.md``, ``topics.json``,
``rules.json``, and ``manifest.json``. Review and activation are explicit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from oft.application.kill_switch import record_audit
from oft.application.prompt_builder import (
    KnowledgeInjection,
    absent_injection,
    build_knowledge_injection,
)
from oft.config import Settings
from oft.domain.knowledge import (
    KNOWLEDGE_ABSENT,
    KNOWLEDGE_MANIFEST_INVALID,
    KNOWLEDGE_NOT_ACTIVE,
    KNOWLEDGE_NOT_REVIEWED,
    KNOWLEDGE_SOURCE_EMPTY,
    KNOWLEDGE_SOURCE_TOO_LARGE,
    LICENSE_NOTE,
    PRIVATE_WARNING,
    REVIEW_APPROVED,
    CompiledArtifact,
    KnowledgeError,
    KnowledgeManifest,
    KnowledgeState,
    absent_state,
    available_state,
    compile_artifact,
    load_sections,
    topic_definitions,
    validate_manifest,
    verify_artifact,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort

PLAYBOOK_DIRNAME = "playbook"
PLAYBOOK_FILE = "playbook.md"
TOPICS_FILE = "topics.json"
RULES_FILE = "rules.json"
MANIFEST_FILE = "manifest.json"
ACTIVE_FILE = "active.json"
LOCATION_FILE = "location.json"

# A directory scan is bounded so a hostile or accidental corpus cannot make the
# compiler read an unbounded amount of data.
MAX_SOURCE_FILES = 500
MAX_SOURCE_FILE_BYTES = 1_048_576


@dataclass(frozen=True)
class KnowledgePaths:
    root: Path
    books: Path
    skills: Path
    playbook: Path

    @property
    def playbook_file(self) -> Path:
        return self.playbook / PLAYBOOK_FILE

    @property
    def topics_file(self) -> Path:
        return self.playbook / TOPICS_FILE

    @property
    def rules_file(self) -> Path:
        return self.playbook / RULES_FILE

    @property
    def manifest_file(self) -> Path:
        return self.playbook / MANIFEST_FILE

    @property
    def active_file(self) -> Path:
        return self.playbook / ACTIVE_FILE


@dataclass(frozen=True)
class KnowledgeBuildResult:
    artifact: CompiledArtifact
    output_dir: Path
    files: tuple[str, ...]
    private_warning: str = PRIVATE_WARNING


def knowledge_paths(settings: Settings) -> KnowledgePaths:
    root = settings.data_dir / "knowledge"
    return KnowledgePaths(
        root=root,
        books=root / "books",
        skills=root / "skills",
        playbook=root / PLAYBOOK_DIRNAME,
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_text(path, text)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class KnowledgeService:
    """Filesystem-backed compiler and lifecycle for the private playbook."""

    def __init__(
        self,
        *,
        settings: Settings,
        clock: ClockPort,
        store: StorePort | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._store = store
        self._paths = knowledge_paths(settings)

    @property
    def paths(self) -> KnowledgePaths:
        return self._paths

    @property
    def private_warning(self) -> str:
        return PRIVATE_WARNING

    # -- build -----------------------------------------------------------

    def build(
        self,
        source: Path,
        *,
        output: Path | None = None,
        created_at: str | None = None,
    ) -> KnowledgeBuildResult:
        source = source.expanduser()
        if source.is_file():
            documents = [source]
            config_dir = source.parent
        elif source.is_dir():
            source_root = source.resolve()
            documents = self._discover_documents(source, source_root)
            config_dir = source
        else:
            raise KnowledgeError(KNOWLEDGE_SOURCE_EMPTY, f"knowledge source not found: {source}")
        if not documents:
            raise KnowledgeError(
                KNOWLEDGE_SOURCE_EMPTY,
                f"no markdown skill documents found in knowledge source: {source}",
            )
        if len(documents) > MAX_SOURCE_FILES:
            raise KnowledgeError(
                KNOWLEDGE_SOURCE_TOO_LARGE,
                f"knowledge source has {len(documents)} files; the limit is {MAX_SOURCE_FILES}",
            )
        for document in documents:
            self._check_source_size(document)

        topics_config = self._optional_json(config_dir / TOPICS_FILE)
        rules_config = self._optional_json(config_dir / RULES_FILE)
        calc_context = (
            topics_config.get("calc_context")
            if isinstance(topics_config, Mapping)
            and isinstance(topics_config.get("calc_context"), Mapping)
            else None
        )
        generated_at = created_at or utc_timestamp(self._clock.now())
        try:
            source_documents = [(path.stem, path.read_text(encoding="utf-8")) for path in documents]
        except (OSError, UnicodeDecodeError) as exc:
            raise KnowledgeError(
                KNOWLEDGE_SOURCE_EMPTY, f"cannot read the knowledge source: {exc}"
            ) from exc
        artifact = compile_artifact(
            source_documents,
            generated_at=generated_at,
            taxonomy=topic_definitions(
                topics_config if isinstance(topics_config, Mapping) else None
            ),
            rules_config=rules_config if isinstance(rules_config, Mapping) else None,
            calc_context=calc_context,
        )

        configured = self._paths.playbook
        target = (output or configured).expanduser()
        self._paths.books.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._paths.skills.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target, 0o700)
        _write_text(target / PLAYBOOK_FILE, artifact.playbook)
        _write_json(target / TOPICS_FILE, artifact.topics)
        _write_json(target / RULES_FILE, artifact.rules)
        _write_json(target / MANIFEST_FILE, artifact.manifest.to_dict())
        self._record_location(configured, target)
        return KnowledgeBuildResult(
            artifact=artifact,
            output_dir=target,
            files=(
                str(target / PLAYBOOK_FILE),
                str(target / TOPICS_FILE),
                str(target / RULES_FILE),
                str(target / MANIFEST_FILE),
            ),
        )

    def _discover_documents(self, source: Path, source_root: Path) -> list[Path]:
        """Markdown files under ``source``, skipping symlinks/outside files."""
        documents: list[Path] = []
        for path in sorted(source.rglob("*.md")):
            if path.name == PLAYBOOK_FILE or path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(source_root):
                continue
            documents.append(path)
        return documents

    def _check_source_size(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise KnowledgeError(
                KNOWLEDGE_SOURCE_EMPTY, f"cannot stat knowledge source {path}: {exc}"
            ) from exc
        if size > MAX_SOURCE_FILE_BYTES:
            raise KnowledgeError(
                KNOWLEDGE_SOURCE_TOO_LARGE,
                f"knowledge source file {path.name} is {size} bytes; "
                f"the limit is {MAX_SOURCE_FILE_BYTES}",
            )

    def _record_location(self, configured: Path, target: Path) -> None:
        """Persist which directory holds the artifact so the lifecycle finds it."""
        location = configured / LOCATION_FILE
        if target.resolve() == configured.resolve():
            if location.exists():
                location.unlink()
            return
        _write_json(
            location,
            {"playbook_dir": str(target.resolve()), "private": True},
        )

    def _resolve_playbook_dir(self) -> Path:
        """Honor a persisted ``--output`` location; otherwise use the config root."""
        pointer = self._paths.playbook / LOCATION_FILE
        if not pointer.is_file():
            return self._paths.playbook
        try:
            payload = _read_json(pointer)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._paths.playbook
        value = payload.get("playbook_dir") if isinstance(payload, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            return self._paths.playbook
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self._paths.playbook / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return self._paths.playbook
        return resolved if resolved.is_dir() else self._paths.playbook

    def _playbook_paths(self) -> KnowledgePaths:
        playbook = self._resolve_playbook_dir()
        if playbook == self._paths.playbook:
            return self._paths
        return replace(self._paths, playbook=playbook)

    def _optional_json(self, path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return _read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KnowledgeError(
                KNOWLEDGE_MANIFEST_INVALID, f"cannot read {path.name}: {exc}"
            ) from exc

    # -- load ------------------------------------------------------------

    def load_artifact(self) -> CompiledArtifact:
        """Load and verify the compiled artifact; absent means absent."""
        paths = self._playbook_paths()
        manifest_path = paths.manifest_file
        playbook_path = paths.playbook_file
        topics_path = paths.topics_file
        rules_path = paths.rules_file
        if not manifest_path.is_file():
            raise KnowledgeError(KNOWLEDGE_ABSENT, "no compiled knowledge manifest is present")
        try:
            manifest_payload = _read_json(manifest_path)
            playbook = playbook_path.read_text(encoding="utf-8")
            topics = _read_json(topics_path)
            rules = _read_json(rules_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KnowledgeError(
                KNOWLEDGE_ABSENT, f"the compiled knowledge artifact is unreadable: {exc}"
            ) from exc
        if not isinstance(manifest_payload, Mapping):
            raise KnowledgeError(KNOWLEDGE_MANIFEST_INVALID, "manifest.json must contain an object")
        manifest = validate_manifest(manifest_payload)
        if not isinstance(topics, Mapping) or not isinstance(rules, Mapping):
            raise KnowledgeError(
                KNOWLEDGE_MANIFEST_INVALID, "topics.json and rules.json must be objects"
            )
        verify_artifact(manifest, playbook, topics, rules)
        sections = load_sections(playbook)
        if not sections:
            raise KnowledgeError(
                KNOWLEDGE_MANIFEST_INVALID, "the playbook contains no section headings"
            )
        return CompiledArtifact(
            playbook=playbook,
            sections=sections,
            topics=dict(topics),
            rules=dict(rules),
            manifest=manifest,
        )

    def load_state(self) -> KnowledgeState:
        try:
            artifact = self.load_artifact()
        except KnowledgeError as exc:
            return absent_state(exc.code)
        return available_state(artifact.manifest, active=self._is_active(artifact.artifact_id))

    def _is_active(self, artifact_id: str) -> bool:
        path = self._playbook_paths().active_file
        if not path.is_file():
            return False
        try:
            payload = _read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(payload, Mapping) and payload.get("artifact_id") == artifact_id

    # -- review / activate ----------------------------------------------

    def review(self, *, reviewed_by: str) -> KnowledgeState:
        reviewer = reviewed_by.strip()
        if not reviewer:
            raise KnowledgeError(KNOWLEDGE_NOT_REVIEWED, "a named human reviewer is required")
        artifact = self.load_artifact()
        payload = artifact.manifest.to_dict()
        payload["reviewed_by"] = reviewer
        payload["review_status"] = REVIEW_APPROVED
        payload["reviewed_at"] = utc_timestamp(self._clock.now())
        manifest = validate_manifest(payload)
        self._write_manifest(manifest)
        self._audit(
            event_type="KNOWLEDGE_REVIEWED",
            artifact_id=manifest.artifact_id,
            actor=reviewer,
            payload={"review_status": REVIEW_APPROVED, "private": True},
        )
        return available_state(manifest, active=self._is_active(manifest.artifact_id))

    def activate(self, *, actor: str = "cli", reason: str = "") -> KnowledgeState:
        artifact = self.load_artifact()
        manifest = artifact.manifest
        if manifest.review_status != REVIEW_APPROVED or not manifest.reviewed_by:
            raise KnowledgeError(
                KNOWLEDGE_NOT_REVIEWED,
                "the knowledge artifact must be reviewed before it can be activated",
            )
        now = utc_timestamp(self._clock.now())
        _write_json(
            self._playbook_paths().active_file,
            {
                "artifact_id": manifest.artifact_id,
                "activated_at": now,
                "actor": actor,
                "reason": reason,
                "reviewed_by": manifest.reviewed_by,
                "license_note": LICENSE_NOTE,
                "private": True,
            },
        )
        self._audit(
            event_type="KNOWLEDGE_ACTIVATED",
            artifact_id=manifest.artifact_id,
            actor=actor,
            payload={"reason": reason, "private": True},
        )
        return available_state(manifest, active=True)

    def _write_manifest(self, manifest: KnowledgeManifest) -> None:
        _write_json(self._playbook_paths().manifest_file, manifest.to_dict())

    def _audit(
        self,
        *,
        event_type: str,
        artifact_id: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        if self._store is None:
            return
        record_audit(
            self._store,
            event_type=event_type,
            actor={"kind": "USER", "id": actor},
            subject={"kind": "KNOWLEDGE_ARTIFACT", "id": artifact_id},
            payload=payload,
            at=utc_timestamp(self._clock.now()),
        )

    # -- injection -------------------------------------------------------

    def injection_for(
        self,
        role: str,
        *,
        mode: str = "A",
        calc_context: dict[str, Any] | None = None,
    ) -> KnowledgeInjection:
        try:
            artifact = self.load_artifact()
        except KnowledgeError as exc:
            return absent_injection(str(role), reason=exc.code)
        if not self._is_active(artifact.artifact_id):
            return absent_injection(str(role), reason=KNOWLEDGE_NOT_ACTIVE)
        return build_knowledge_injection(
            artifact, role=str(role), mode=mode, calc_context=calc_context
        )


__all__ = [
    "ACTIVE_FILE",
    "LOCATION_FILE",
    "MANIFEST_FILE",
    "MAX_SOURCE_FILES",
    "MAX_SOURCE_FILE_BYTES",
    "PLAYBOOK_DIRNAME",
    "PLAYBOOK_FILE",
    "RULES_FILE",
    "TOPICS_FILE",
    "KnowledgeBuildResult",
    "KnowledgePaths",
    "KnowledgeService",
    "knowledge_paths",
]
