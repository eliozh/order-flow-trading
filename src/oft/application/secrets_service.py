"""Write-only secret lifecycle for provider keys (slice 35).

Keys live in the OS keyring when one is available, otherwise in an
encrypted-file fallback (``$OFT_DATA/secrets.enc``, mode 0600, atomic replace)
that requires ``OFT_MASTER_KEY``. If neither exists the write is refused and no
plaintext file is ever created. SQLite stores only ``secrets_meta`` metadata:
provider/name, a SHA-256 fingerprint, the last four characters, and provenance.

The API, CLI text, audit payloads, logs, and errors expose the mask only and
never return a key value. Two in-process accessors exist and are deliberately
not exposed as endpoints: :meth:`SecretsService.resolve_key` for provider
construction and :meth:`SecretsService.redaction_values` for scrubbing provider
error text.

This module deliberately stays cohesive across the secret-lifecycle boundary
and is allowed to exceed the 800-line soft ceiling: backend selection,
provenance routing, and the atomic metadata/backend rollback share one security
invariant, and splitting them would spread that invariant across files.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from oft.adapters.keyring_secrets import KeyringSecretBackend
from oft.domain.llm_settings import (
    PROVIDER_KEY_FIELDS,
    PROVIDERS,
    SECRET_ADDED_EVENT,
    SECRET_DELETED_EVENT,
    SECRET_NAME,
    SECRET_ROTATED_EVENT,
    SECRET_SUBJECT_KIND,
    LLMSettingsValidationError,
    fingerprint_secret,
    last4,
    mask_secret,
    validate_api_key,
    validate_provider,
)
from oft.domain.telegram import (
    TELEGRAM_SUBJECT,
    TELEGRAM_TOKEN_NAME,
    validate_telegram_token,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.secrets import SecretStoreUnavailable
from oft.ports.store import (
    AuditEventDraft,
    SecretChangePlan,
    SecretMetaRecord,
    StorePort,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from oft.config import Settings

LOGGER = structlog.get_logger(__name__)
ENCRYPTED_FILE_NAME = "secrets.enc"
MASTER_KEY_ENV = "OFT_MASTER_KEY"
MIN_MASTER_KEY_LENGTH = 32
UNKNOWN_BACKEND = "unknown"
DEFAULT_UNAVAILABLE_REASON = (
    "no secure secret store is available; install an OS keyring backend "
    "or set OFT_MASTER_KEY for the encrypted-file store"
)
WEAK_MASTER_KEY_REASON = f"OFT_MASTER_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters"
# 2^15 with r=8 (~32 MiB) is the current floor for a passphrase-derived key;
# the decrypted map is cached per (path, mtime, master) so the KDF does not run
# on every provider resolution.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_FILE_VERSION = 1

# Serialises the whole metadata+backend lifecycle inside one process.
_SECRET_LIFECYCLE_LOCK = threading.RLock()
# Serialises direct encrypted-file read-modify-write inside one process.
_FILE_RW_LOCK = threading.RLock()
_DECRYPT_CACHE: dict[Path, tuple[tuple[int, int, str], dict[str, str]]] = {}
_DECRYPT_CACHE_LOCK = threading.Lock()


class SecretBackend(Protocol):
    """A place to keep the plaintext value; never part of the SQLite schema."""

    kind: str

    def available(self) -> bool: ...

    def get(self, reference: str) -> str | None: ...

    def set(self, reference: str, value: str) -> None: ...

    def delete(self, reference: str) -> None: ...


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """An exclusive advisory lock shared by every process using this store.

    ``fcntl`` is unavailable on Windows; there the in-process lock still
    serialises writers, and the store is a single-user desktop fallback.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.ExitStack() as stack:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows-only fallback
            yield
            return
        handle = stack.enter_context(open(path, "a+", encoding="utf-8"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _derive_fernet_key(master: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(master.encode("utf-8")))


@contextmanager
def _secret_lifecycle_lock(lock_path: Path | None) -> Iterator[None]:
    """The process lock plus (when possible) the cross-process advisory lock."""
    with _SECRET_LIFECYCLE_LOCK:
        if lock_path is None:
            yield
        else:
            with _file_lock(lock_path):
                yield


class EncryptedFileSecretBackend(SecretBackend):
    """Encrypted-file fallback; requires ``OFT_MASTER_KEY``.

    The file is a JSON envelope containing a random scrypt salt and the Fernet
    ciphertext of the whole reference→value map. Writes go to a temp file in the
    same directory, are flushed and fsynced, then atomically replaced with mode
    0600 so a reader never sees a partial or plaintext file.
    """

    kind = "encrypted_file"

    def __init__(self, *, data_dir: Path, environ: Mapping[str, str] | None = None) -> None:
        self._data_dir = data_dir
        self._environ = os.environ if environ is None else environ

    @property
    def path(self) -> Path:
        return self._data_dir / ENCRYPTED_FILE_NAME

    @property
    def _lock_path(self) -> Path:
        return self._data_dir / f"{ENCRYPTED_FILE_NAME}.lock"

    def available(self) -> bool:
        master = self._master()
        return master is not None and len(master) >= MIN_MASTER_KEY_LENGTH

    def _master(self) -> str | None:
        value = self._environ.get(MASTER_KEY_ENV)
        return value if value is not None and value.strip() else None

    def _require_master(self) -> str:
        master = self._master()
        if master is None:
            raise SecretStoreUnavailable(
                "the encrypted secret store requires OFT_MASTER_KEY to be set",
                reason="backend_unavailable",
            )
        if len(master) < MIN_MASTER_KEY_LENGTH:
            raise SecretStoreUnavailable(
                f"OFT_MASTER_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters",
                reason="backend_unavailable",
            )
        return master

    def _cache_identity(self, master: str) -> tuple[int, int, str]:
        stat = self.path.stat()
        digest = hashlib.sha256(master.encode("utf-8")).hexdigest()
        return (stat.st_mtime_ns, stat.st_size, digest)

    def _load(self) -> dict[str, str]:
        master = self._require_master()
        if not self.path.exists():
            return {}
        identity = self._cache_identity(master)
        with _DECRYPT_CACHE_LOCK:
            cached = _DECRYPT_CACHE.get(self.path)
            if cached is not None and cached[0] == identity:
                return dict(cached[1])
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if envelope.get("version") != _FILE_VERSION:
                raise SecretStoreUnavailable(
                    "the encrypted secret store has an unknown format version",
                    reason="decrypt_failed",
                )
            salt = base64.b64decode(envelope["salt"])
            token = envelope["token"].encode("ascii")
            plaintext = Fernet(_derive_fernet_key(master, salt)).decrypt(token)
            values = json.loads(plaintext.decode("utf-8"))
        except SecretStoreUnavailable:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidToken) as exc:
            raise SecretStoreUnavailable(
                "the encrypted secret store cannot be decrypted with this master key",
                reason="decrypt_failed",
            ) from exc
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise SecretStoreUnavailable(
                "the encrypted secret store is corrupt", reason="decrypt_failed"
            )
        with _DECRYPT_CACHE_LOCK:
            _DECRYPT_CACHE[self.path] = (identity, dict(values))
        return values

    def _store(self, values: Mapping[str, str]) -> None:
        master = self._require_master()
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        salt = os.urandom(16)
        token = Fernet(_derive_fernet_key(master, salt)).encrypt(
            json.dumps(dict(values), sort_keys=True).encode("utf-8")
        )
        payload = json.dumps(
            {
                "version": _FILE_VERSION,
                "salt": base64.b64encode(salt).decode("ascii"),
                "token": token.decode("ascii"),
            },
            sort_keys=True,
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            dir=self._data_dir, prefix=".secrets-", suffix=".tmp"
        )
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        with _DECRYPT_CACHE_LOCK:
            _DECRYPT_CACHE.pop(self.path, None)

    def get(self, reference: str) -> str | None:
        return self._load().get(reference)

    def set(self, reference: str, value: str) -> None:
        with _FILE_RW_LOCK, _file_lock(self._lock_path):
            values = self._load()
            values[reference] = value
            self._store(values)

    def delete(self, reference: str) -> None:
        with _FILE_RW_LOCK, _file_lock(self._lock_path):
            values = self._load()
            if values.pop(reference, None) is None:
                return
            self._store(values)


@dataclass(frozen=True)
class SecretBackendSelection:
    """The chosen backend plus why none was chosen (composition never raises)."""

    backend: SecretBackend | None
    reason: str | None = None


def select_backend(settings: Settings) -> SecretBackendSelection:
    """Choose a secure backend without raising during composition.

    A weak ``OFT_MASTER_KEY`` or a host with no keyring is recorded as a reason
    and only surfaces when a read/write actually needs the store, so ``oft
    status`` and the daemon can still start and report the state.
    """
    keyring_backend = KeyringSecretBackend()
    try:
        if keyring_backend.available():
            return SecretBackendSelection(keyring_backend)
    except Exception:  # noqa: BLE001 - a probe failure means "not available"
        LOGGER.warning("secret_keyring_probe_failed", exc_info=True)
    file_backend = EncryptedFileSecretBackend(data_dir=settings.data_dir)
    master = file_backend._master()  # noqa: SLF001 - the selection probe
    if master is not None and len(master) < MIN_MASTER_KEY_LENGTH:
        return SecretBackendSelection(None, WEAK_MASTER_KEY_REASON)
    if master is None and file_backend.path.exists():
        LOGGER.warning(
            "secret_store_locked_out",
            reason="secrets.enc exists but OFT_MASTER_KEY is not set",
        )
    try:
        if file_backend.available():
            return SecretBackendSelection(file_backend)
    except Exception:  # noqa: BLE001 - a probe failure means "not available"
        LOGGER.warning("secret_file_probe_failed", exc_info=True)
    return SecretBackendSelection(None, DEFAULT_UNAVAILABLE_REASON)


def default_backend(settings: Settings) -> SecretBackend | None:
    """The OS keyring first, then the encrypted file, else ``None``.

    Tests and the composition root may inject a backend explicitly; this
    function is the single production selection point. It never raises: a weak
    ``OFT_MASTER_KEY`` is recorded by :func:`select_backend` and raised by the
    service only when a read or write needs the store.
    """
    return select_backend(settings).backend


# Captured so ``SecretsService`` can tell a production selection from a test
# monkeypatch. An injected backend must never cause another backend to be
# constructed or touched (notably the developer's real OS keyring).
_ORIGINAL_DEFAULT_BACKEND = default_backend
_BUILTIN_BACKENDS = (KeyringSecretBackend, EncryptedFileSecretBackend)


def backend_for_kind(kind: str, settings: Settings | None) -> SecretBackend | None:
    """Construct one available backend by recorded provenance.

    Used to read/delete a key from the backend it was written to even when a
    different backend is selected now (for example, a keyring appeared after a
    key was stored in the encrypted file). Returns ``None`` when that backend is
    not available.
    """
    if kind == KeyringSecretBackend.kind:
        backend: SecretBackend = KeyringSecretBackend()
    elif kind == EncryptedFileSecretBackend.kind and settings is not None:
        backend = EncryptedFileSecretBackend(data_dir=settings.data_dir)
    else:
        return None
    try:
        return backend if backend.available() else None
    except Exception:  # noqa: BLE001 - a probe failure means "not available"
        return None


def _selection_reason(settings: Settings) -> str:
    """The human reason recorded when no backend was selected."""
    return select_backend(settings).reason or DEFAULT_UNAVAILABLE_REASON


@dataclass(frozen=True)
class SecretStatus:
    """The only key-shaped data a surface may render.

    ``last4`` exists for the Telegram masked status; the LLM ``to_contract``
    shape is deliberately unchanged.
    """

    provider: str
    configured: bool
    key_source: str
    masked: str | None
    fingerprint: str | None
    updated_at: str | None
    updated_by: str | None
    last4: str | None = None

    def to_contract(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "key_source": self.key_source,
            "masked": self.masked,
            "fingerprint": self.fingerprint,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True)
class _BackendRead:
    """A best-effort read: ``readable=False`` means absent is unknown, not empty."""

    value: str | None
    readable: bool


class SecretsService:
    """Write, rotate, and delete provider keys without returning a value to any
    API, CLI, or UI surface."""

    def __init__(
        self,
        *,
        store: StorePort,
        settings: Settings | None,
        clock: ClockPort,
        backend: SecretBackend | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock
        self._environ = environ
        patched_default = default_backend is not _ORIGINAL_DEFAULT_BACKEND
        selected: SecretBackend | None
        if backend is not None:
            selected = backend
        elif settings is not None:
            selected = default_backend(settings)
        else:
            selected = None
        self._backend = selected
        # An injected backend (constructor or monkeypatched ``default_backend``)
        # isolates the service: it must never construct or call a settings-
        # derived keyring/encrypted-file backend. That keeps tests away from the
        # developer's real OS keyring.
        self._isolated_backend = (
            backend is not None
            or patched_default
            or (selected is not None and not isinstance(selected, _BUILTIN_BACKENDS))
        )
        if selected is not None:
            self._unavailable_reason: str | None = None
        elif self._isolated_backend or settings is None:
            self._unavailable_reason = DEFAULT_UNAVAILABLE_REASON
        else:
            self._unavailable_reason = _selection_reason(settings)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def reference(provider: str) -> str:
        return f"llm.{provider}.{SECRET_NAME}"

    @staticmethod
    def reference_for(subject: str, name: str) -> str:
        """The backend reference for one generic ``(subject, name)`` secret.

        An LLM provider pair must address the *same* backend reference as the
        dedicated ``set_key``/``status``/``delete_key`` methods, otherwise the
        generic path would orphan the real value while deleting its metadata.
        """
        if name == SECRET_NAME and subject in PROVIDERS:
            return SecretsService.reference(subject)
        return f"{subject}.{name}"

    def _env_key(self, provider: str) -> str | None:
        attribute = PROVIDER_KEY_FIELDS.get(provider)
        if attribute is None:
            return None
        secret = getattr(self._settings, attribute, None)
        getter = getattr(secret, "get_secret_value", None)
        value = getter() if callable(getter) else secret
        return value if isinstance(value, str) and value.strip() else None

    def _env_secret(self, subject: str, name: str) -> str | None:
        """The environment fallback for one generic secret pair."""
        if subject == TELEGRAM_SUBJECT and name == TELEGRAM_TOKEN_NAME:
            secret = getattr(self._settings, "telegram_bot_token", None)
            getter = getattr(secret, "get_secret_value", None)
            value = getter() if callable(getter) else secret
            return value if isinstance(value, str) and value.strip() else None
        if name == SECRET_NAME and subject in PROVIDERS:
            return self._env_key(subject)
        return None

    def _available_backends(self) -> tuple[SecretBackend, ...]:
        """Every usable backend, the selected/injected one first, by kind."""
        if self._isolated_backend:
            backend = self._backend
            if backend is None:
                return ()
            try:
                return (backend,) if backend.available() else ()
            except Exception:  # noqa: BLE001 - a probe failure means "not available"
                return ()
        candidates: list[SecretBackend | None] = []
        if self._backend is not None:
            candidates.append(self._backend)
        if self._settings is not None:
            for kind in (KeyringSecretBackend.kind, EncryptedFileSecretBackend.kind):
                candidates.append(backend_for_kind(kind, self._settings))
        available: list[SecretBackend] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate is None or candidate.kind in seen:
                continue
            try:
                usable = candidate.available()
            except Exception:  # noqa: BLE001 - a probe failure means "not available"
                usable = False
            if usable:
                seen.add(candidate.kind)
                available.append(candidate)
        return tuple(available)

    def _backend_for_kind(self, kind: str) -> SecretBackend | None:
        for backend in self._available_backends():
            if backend.kind == kind:
                return backend
        return None

    def _read_backend_ref(
        self, backend: SecretBackend, reference: str, *, log_key: str
    ) -> str | None:
        try:
            value = backend.get(reference)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            LOGGER.error("secret_store_read_failed", provider=log_key)
            raise SecretStoreUnavailable(
                "the secure secret store is unavailable", reason="backend_unavailable"
            ) from exc
        return value if isinstance(value, str) and value else None

    def _read_backend_best_effort_ref(
        self, backend: SecretBackend, reference: str, *, context: str, log_key: str
    ) -> _BackendRead:
        """Read without raising, preserving absent-vs-unreadable.

        Rollback must never delete a value it could not read, and a skipped
        restoration must be logged instead of silently disappearing.
        """
        try:
            return _BackendRead(
                value=self._read_backend_ref(backend, reference, log_key=log_key),
                readable=True,
            )
        except SecretStoreUnavailable as exc:
            LOGGER.error(
                "secret_backend_best_effort_read_failed",
                provider=log_key,
                backend=backend.kind,
                context=context,
                reason=exc.reason,
            )
            return _BackendRead(value=None, readable=False)

    def _stored_value(self, provider: str) -> str | None:
        return self._stored_value_ref(self.reference(provider), log_key=provider)

    def _stored_value_ref(self, reference: str, *, log_key: str) -> str | None:
        """The first stored value across every available backend (best effort)."""
        for backend in self._available_backends():
            read = self._read_backend_best_effort_ref(
                backend, reference, context="redaction", log_key=log_key
            )
            if read.value is not None:
                return read.value
        return None

    def _stored_with_backend_ref(
        self, reference: str, meta: SecretMetaRecord | None, *, log_key: str
    ) -> tuple[str | None, SecretBackend | None]:
        """Read a stored value by recorded provenance; fail closed.

        A metadata row plus a missing value, an unavailable backend, or a
        decryption failure is a distinct state from "no key". Returning
        ``None`` would silently fall back to an environment key the operator
        does not have in effect, so the runtime refuses instead.
        """
        if meta is None:
            return None, None
        recorded = (meta.backend or UNKNOWN_BACKEND).strip().lower()
        if recorded and recorded != UNKNOWN_BACKEND:
            backend = self._backend_for_kind(recorded)
            if backend is None:
                raise SecretStoreUnavailable(
                    "the stored provider key exists but its secret backend is unavailable",
                    reason="backend_unavailable",
                )
            value = self._read_backend_ref(backend, reference, log_key=log_key)
            if value is None:
                raise SecretStoreUnavailable(
                    "the stored provider key exists but its value is missing",
                    reason="value_missing",
                )
            return value, backend
        # Legacy rows written before provenance was recorded: probe every
        # available backend once and use the first value found.
        backends = self._available_backends()
        if not backends:
            raise SecretStoreUnavailable(
                "the stored provider key exists but the secret store is unavailable",
                reason="backend_unavailable",
            )
        for backend in backends:
            value = self._read_backend_ref(backend, reference, log_key=log_key)
            if value is not None:
                return value, backend
        raise SecretStoreUnavailable(
            "the stored provider key exists but its value is missing", reason="value_missing"
        )

    def _lifecycle_guard(self) -> AbstractContextManager[None]:
        """Serialise a full backend+metadata secret mutation.

        The in-process lock plus an advisory file lock in ``$OFT_DATA`` keeps a
        CLI process and the daemon from interleaving a read-modify-write, so a
        rotation cannot lose the other writer's key or mismatch the metadata
        fingerprint from the stored value.
        """
        data_dir = getattr(self._settings, "data_dir", None)
        lock_path = Path(data_dir) / "secrets.lock" if data_dir is not None else None
        return _secret_lifecycle_lock(lock_path)

    def _require_backend(self) -> SecretBackend:
        backend = self._backend
        if backend is not None:
            try:
                if backend.available():
                    return backend
            except Exception:  # noqa: BLE001 - a probe failure means "not available"
                pass
            LOGGER.error(
                "secret_backend_selected_unavailable",
                backend=backend.kind,
                reason="selected_backend_unavailable",
            )
            raise SecretStoreUnavailable(
                "the secure secret store is unavailable",
                reason="selected_backend_unavailable",
            )
        raise SecretStoreUnavailable(
            self._unavailable_reason or DEFAULT_UNAVAILABLE_REASON,
            reason="backend_unavailable",
        )

    # -- reads -----------------------------------------------------------

    def status(self, provider: str) -> SecretStatus:
        name = validate_provider(provider) or ""
        return self._status_for(name, SECRET_NAME, self.reference(name), log_key=name)

    def status_for(self, subject: str, name: str) -> SecretStatus:
        """Status for one generic ``(subject, name)`` secret (for example
        ``telegram/bot_token``), exposing only masked metadata."""
        normalized_subject = (subject or "").strip().lower()
        normalized_name = (name or "").strip().lower()
        reference = self.reference_for(normalized_subject, normalized_name)
        return self._status_for(
            normalized_subject,
            normalized_name,
            reference,
            log_key=f"{normalized_subject}.{normalized_name}",
        )

    def _status_for(
        self,
        meta_provider: str,
        meta_name: str,
        reference: str,
        *,
        log_key: str,
    ) -> SecretStatus:
        meta = self._store.get_secret_meta(meta_provider, meta_name)
        try:
            stored, stored_backend = self._stored_with_backend_ref(reference, meta, log_key=log_key)
        except SecretStoreUnavailable as exc:
            LOGGER.error(
                "secret_store_unavailable",
                provider=log_key,
                reason=exc.reason,
            )
            return SecretStatus(
                provider=meta_provider,
                configured=False,
                key_source="unavailable",
                masked=None,
                fingerprint=meta.fingerprint if meta is not None else None,
                updated_at=meta.updated_at if meta is not None else None,
                updated_by=meta.updated_by if meta is not None else None,
                last4=meta.last4 if meta is not None else None,
            )
        if meta is not None and stored is not None:
            key_source = (
                stored_backend.kind if stored_backend is not None else (meta.backend or "secret")
            )
            return SecretStatus(
                provider=meta_provider,
                configured=True,
                key_source=key_source,
                masked=mask_secret(stored),
                fingerprint=meta.fingerprint,
                updated_at=meta.updated_at,
                updated_by=meta.updated_by,
                last4=meta.last4,
            )
        env_key = self._env_secret(meta_provider, meta_name)
        if env_key is not None:
            return SecretStatus(
                provider=meta_provider,
                configured=True,
                key_source="env",
                masked=None,
                fingerprint=None,
                updated_at=None,
                updated_by=None,
                last4=None,
            )
        return SecretStatus(
            provider=meta_provider,
            configured=False,
            key_source="unset",
            masked=None,
            fingerprint=None,
            updated_at=None,
            updated_by=None,
            last4=None,
        )

    def list_statuses(self) -> tuple[SecretStatus, ...]:
        return tuple(self.status(provider) for provider in PROVIDERS)

    def resolve_key(self, provider: str) -> str | None:
        """The stored key (preferred) or the provider's environment key.

        An in-process accessor for provider construction only; no API, CLI, or
        UI surface returns its value. Never consults another provider: deleting
        the selected provider's key surfaces the stable configuration error
        instead of a silent fallback. A metadata row whose value cannot be read
        raises :class:`SecretStoreUnavailable` rather than falling back to the
        environment key.
        """
        name = (provider or "").strip().lower()
        if name not in PROVIDERS:
            return None
        meta = self._store.get_secret_meta(name, SECRET_NAME)
        stored = self._stored_with_backend_ref(self.reference(name), meta, log_key=name)[0]
        if stored is not None:
            return stored
        return self._env_key(name)

    def resolve_secret(self, subject: str, name: str) -> str | None:
        """The stored value (preferred) or the environment fallback.

        In-process only: used by the Telegram adapter's per-call token resolver
        and provider construction. No surface returns the value.
        """
        normalized_subject = (subject or "").strip().lower()
        normalized_name = (name or "").strip().lower()
        meta = self._store.get_secret_meta(normalized_subject, normalized_name)
        reference = self.reference_for(normalized_subject, normalized_name)
        stored = self._stored_with_backend_ref(
            reference, meta, log_key=f"{normalized_subject}.{normalized_name}"
        )[0]
        if stored is not None:
            return stored
        return self._env_secret(normalized_subject, normalized_name)

    def redaction_values(self, *, include_providers: bool = True) -> tuple[str, ...]:
        """In-process values provider error text must never contain.

        Used only for scrubbing provider-controlled text; never exposed.
        ``include_providers=False`` resolves only the Telegram token so the
        composition root can seed log redaction without eagerly reading every
        provider key (keyring calls / the scrypt KDF) on every CLI command.
        """
        values: list[str] = []
        if include_providers:
            for provider in PROVIDERS:
                stored = self._stored_value(provider)
                if stored:
                    values.append(stored)
                env_key = self._env_key(provider)
                if env_key:
                    values.append(env_key)
        telegram_reference = self.reference_for(TELEGRAM_SUBJECT, TELEGRAM_TOKEN_NAME)
        stored_token = self._stored_value_ref(
            telegram_reference, log_key=f"{TELEGRAM_SUBJECT}.{TELEGRAM_TOKEN_NAME}"
        )
        if stored_token:
            values.append(stored_token)
        env_token = self._env_secret(TELEGRAM_SUBJECT, TELEGRAM_TOKEN_NAME)
        if env_token:
            values.append(env_token)
        return tuple(values)

    # -- writes ----------------------------------------------------------

    def set_key(self, provider: str, value: object, *, actor: str) -> SecretStatus:
        with self._lifecycle_guard():
            return self._set_key_locked(provider, value, actor=actor)

    def set_secret(self, subject: str, name: str, value: object, *, actor: str) -> SecretStatus:
        """Store or rotate one generic ``(subject, name)`` secret.

        The Telegram bot token is validated against the Bot API shape; any
        other pair keeps the existing provider-key rule.
        """
        normalized_subject = (subject or "").strip().lower()
        normalized_name = (name or "").strip().lower()
        if not normalized_subject or not normalized_name:
            raise LLMSettingsValidationError(
                "a secret subject and name are required",
                details=[
                    {
                        "field": "subject" if not normalized_subject else "name",
                        "code": "required",
                        "message": "a secret subject and name are required",
                    }
                ],
            )
        if (normalized_subject, normalized_name) == (
            TELEGRAM_SUBJECT,
            TELEGRAM_TOKEN_NAME,
        ):
            normalized = validate_telegram_token(value)
        else:
            normalized = validate_api_key(value)
        reference = self.reference_for(normalized_subject, normalized_name)
        log_key = f"{normalized_subject}.{normalized_name}"
        with self._lifecycle_guard():
            return self._write_secret(
                meta_provider=normalized_subject,
                meta_name=normalized_name,
                reference=reference,
                normalized=normalized,
                actor=actor,
                subject_id=log_key,
                log_key=log_key,
            )

    def _set_key_locked(self, provider: str, value: object, *, actor: str) -> SecretStatus:
        name = validate_provider(provider)
        if name is None:
            raise LLMSettingsValidationError(
                "provider is required",
                details=[
                    {"field": "provider", "code": "required", "message": "provider is required"}
                ],
            )
        normalized = validate_api_key(value)
        return self._write_secret(
            meta_provider=name,
            meta_name=SECRET_NAME,
            reference=self.reference(name),
            normalized=normalized,
            actor=actor,
            subject_id=f"{name}.{SECRET_NAME}",
            log_key=name,
        )

    def _write_secret(
        self,
        *,
        meta_provider: str,
        meta_name: str,
        reference: str,
        normalized: str,
        actor: str,
        subject_id: str,
        log_key: str,
    ) -> SecretStatus:
        backend = self._require_backend()
        # The previous value on the backend being written, for a metadata
        # rollback. Best effort but keep absent-vs-unreadable distinct: a
        # rollback must never delete a value it could not read.
        previous_read = self._read_backend_best_effort_ref(
            backend, reference, context="set_previous", log_key=log_key
        )
        previous = previous_read.value
        prior_meta = self._store.get_secret_meta(meta_provider, meta_name)
        try:
            backend.set(reference, normalized)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            raise SecretStoreUnavailable(
                "the secure secret store rejected the write", reason="backend_unavailable"
            ) from exc

        timestamp = utc_timestamp(self._clock.now())
        digest = fingerprint_secret(normalized)
        suffix = last4(normalized)

        def _build(current: SecretMetaRecord | None) -> SecretChangePlan:
            event_type = SECRET_ROTATED_EVENT if current is not None else SECRET_ADDED_EVENT
            record = SecretMetaRecord(
                provider=meta_provider,
                name=meta_name,
                fingerprint=digest,
                last4=suffix,
                updated_at=timestamp,
                updated_by=actor,
                # Record provenance so a later read/delete targets the backend
                # that actually holds this value, even if keyring availability
                # changes.
                backend=backend.kind,
            )
            audit = AuditEventDraft(
                event_type=event_type,
                actor={"kind": "USER", "id": actor},
                subject={"kind": SECRET_SUBJECT_KIND, "id": subject_id},
                # Only non-secret metadata: a hash, provider, and name.
                payload={
                    "provider": meta_provider,
                    "name": meta_name,
                    "fingerprint": digest,
                    "source": backend.kind,
                },
                created_at=timestamp,
            )
            return SecretChangePlan(record=record, audit=audit)

        try:
            self._store.save_secret_change(meta_provider, meta_name, _build)
        except BaseException:
            # Never leave the backend ahead of the metadata after a DB failure,
            # but never clobber a value a newer write already installed.
            try:
                current = backend.get(reference)
            except Exception:  # noqa: BLE001 - the original failure wins
                current = None
                LOGGER.error("secret_backend_rollback_skipped", provider=log_key)
            if current == normalized:
                if not previous_read.readable:
                    # The prior value could be absent or unreadable; deleting
                    # here could destroy a key we never saw.
                    LOGGER.error(
                        "secret_backend_rollback_unavailable",
                        provider=log_key,
                        backend=backend.kind,
                        reason="previous_unreadable",
                    )
                else:
                    try:
                        if previous is None:
                            backend.delete(reference)
                        else:
                            backend.set(reference, previous)
                    except Exception:  # noqa: BLE001 - the original failure wins
                        LOGGER.error("secret_backend_rollback_failed", provider=log_key)
            raise
        LOGGER.info(
            "secret_stored",
            provider=log_key,
            rotated=previous is not None or not previous_read.readable,
            previous_readable=previous_read.readable,
        )
        self._purge_superseded_backends(
            reference, log_key=log_key, current_kind=backend.kind, prior_meta=prior_meta
        )
        return self._status_for(meta_provider, meta_name, reference, log_key=log_key)

    def _purge_superseded_backends(
        self,
        reference: str,
        *,
        log_key: str,
        current_kind: str,
        prior_meta: SecretMetaRecord | None,
    ) -> None:
        """Best-effort delete of a rotated key from every other backend (M-F).

        A prior keyring value must not survive a rotation to the encrypted file
        (or vice versa). Cleanup never fails the committed rotation and never
        logs a value.
        """
        available = self._available_backends()
        for other in available:
            if other.kind == current_kind:
                continue
            try:
                self._delete_on(other, reference)
            except SecretStoreUnavailable as exc:
                LOGGER.warning(
                    "secret_rotation_residue_unavailable",
                    provider=log_key,
                    backend=other.kind,
                    reason=exc.reason,
                )
        prior_kind = (
            (prior_meta.backend if prior_meta is not None else UNKNOWN_BACKEND).strip().lower()
        )
        if (
            prior_kind
            and prior_kind != UNKNOWN_BACKEND
            and prior_kind != current_kind
            and all(backend.kind != prior_kind for backend in available)
        ):
            LOGGER.warning(
                "secret_rotation_residue_unavailable",
                provider=log_key,
                backend=prior_kind,
                reason="backend_unavailable",
            )

    def delete_key(self, provider: str, *, actor: str) -> SecretStatus:
        with self._lifecycle_guard():
            return self._delete_key_locked(provider, actor=actor)

    def delete_secret(self, subject: str, name: str, *, actor: str) -> SecretStatus:
        """Delete one generic ``(subject, name)`` secret from every backend."""
        normalized_subject = (subject or "").strip().lower()
        normalized_name = (name or "").strip().lower()
        reference = self.reference_for(normalized_subject, normalized_name)
        log_key = f"{normalized_subject}.{normalized_name}"
        with self._lifecycle_guard():
            return self._delete_secret_locked(
                meta_provider=normalized_subject,
                meta_name=normalized_name,
                reference=reference,
                log_key=log_key,
                actor=actor,
            )

    def _delete_on(self, backend: SecretBackend, reference: str) -> None:
        try:
            backend.delete(reference)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            raise SecretStoreUnavailable(
                "the secure secret store rejected the delete", reason="backend_unavailable"
            ) from exc

    def _delete_key_locked(self, provider: str, *, actor: str) -> SecretStatus:
        name = validate_provider(provider) or ""
        return self._delete_secret_locked(
            meta_provider=name,
            meta_name=SECRET_NAME,
            reference=self.reference(name),
            log_key=name,
            actor=actor,
        )

    def _delete_secret_locked(
        self,
        *,
        meta_provider: str,
        meta_name: str,
        reference: str,
        log_key: str,
        actor: str,
    ) -> SecretStatus:
        meta = self._store.get_secret_meta(meta_provider, meta_name)
        available = self._available_backends()
        if meta is None:
            # A crash between the backend write and the metadata commit (or a
            # restored database) can leave an orphaned backend value. Revoke it
            # from every available backend; it is a harmless no-op when absent.
            for backend in available:
                self._delete_on(backend, reference)
            return self._status_for(meta_provider, meta_name, reference, log_key=log_key)

        recorded = (meta.backend or UNKNOWN_BACKEND).strip().lower()
        if recorded and recorded != UNKNOWN_BACKEND:
            required = self._backend_for_kind(recorded)
            if required is None:
                # The recorded backend is gone; the ciphertext may still exist,
                # so refusing is the only truthful outcome (no false revoke).
                raise SecretStoreUnavailable(
                    "the stored provider key exists but its secret backend is unavailable",
                    reason="backend_unavailable",
                )
            targets: list[SecretBackend] = [required]
        else:
            # H-B: a legacy ``unknown`` row is only deletable when a readable
            # value is actually found. A false revocation must not be audited
            # while the value may still exist (or the store is unavailable).
            self._stored_with_backend_ref(reference, meta, log_key=log_key)
            if not available:
                raise SecretStoreUnavailable(
                    "the stored provider key exists but the secret store is unavailable",
                    reason="backend_unavailable",
                )
            targets = list(available)

        # Also revoke any other available backend so a stale ciphertext (for
        # example a file written before a keyring appeared) is never left behind.
        for backend in available:
            if all(backend.kind != target.kind for target in targets):
                targets.append(backend)

        previous_by_kind = {
            backend.kind: self._read_backend_best_effort_ref(
                backend, reference, context="delete_rollback", log_key=log_key
            )
            for backend in targets
        }
        for backend in targets:
            self._delete_on(backend, reference)

        timestamp = utc_timestamp(self._clock.now())

        def _build(current: SecretMetaRecord | None) -> SecretChangePlan | None:
            if current is None:
                return None
            audit = AuditEventDraft(
                event_type=SECRET_DELETED_EVENT,
                actor={"kind": "USER", "id": actor},
                subject={"kind": SECRET_SUBJECT_KIND, "id": f"{meta_provider}.{meta_name}"},
                payload={"provider": meta_provider, "name": meta_name},
                created_at=timestamp,
            )
            return SecretChangePlan(record=None, audit=audit)

        try:
            self._store.save_secret_change(meta_provider, meta_name, _build)
        except BaseException:
            # Restore only when the delete is still the newest backend state.
            for backend in targets:
                current = self._read_backend_best_effort_ref(
                    backend, reference, context="delete_rollback_check", log_key=log_key
                )
                previous = previous_by_kind.get(backend.kind)
                if previous is None or not previous.readable:
                    if previous is not None:
                        LOGGER.error(
                            "secret_backend_rollback_unavailable",
                            provider=log_key,
                            backend=backend.kind,
                            reason="previous_unreadable",
                        )
                    continue
                if current.readable and current.value is None and previous.value is not None:
                    try:
                        backend.set(reference, previous.value)
                    except Exception:  # noqa: BLE001 - the original failure wins
                        LOGGER.error("secret_backend_rollback_failed", provider=log_key)
            raise
        LOGGER.info("secret_deleted", provider=log_key)
        return self._status_for(meta_provider, meta_name, reference, log_key=log_key)


__all__ = [
    "DEFAULT_UNAVAILABLE_REASON",
    "ENCRYPTED_FILE_NAME",
    "EncryptedFileSecretBackend",
    "MASTER_KEY_ENV",
    "SecretBackend",
    "SecretBackendSelection",
    "SecretStatus",
    "SecretStoreUnavailable",
    "SecretsService",
    "backend_for_kind",
    "default_backend",
    "select_backend",
]
