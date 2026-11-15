"""Write-only secret lifecycle for provider keys (slice 35).

Keys live in the OS keyring when one is available, otherwise in an
encrypted-file fallback (``$OFT_DATA/secrets.enc``, mode 0600, atomic replace)
that requires ``OFT_MASTER_KEY``. If neither exists the write is refused and no
plaintext file is ever created. SQLite stores only ``secrets_meta`` metadata:
provider/name, a SHA-256 fingerprint, the last four characters, and provenance.

No public method returns a key value. :meth:`SecretsService.resolve_key` is the
only accessor and is reserved for in-process provider construction; the API,
CLI text, audit payloads, logs, and errors expose the mask only.
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
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
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


class SecretStoreUnavailable(Exception):
    """No secure store could accept the key; surfaced as ``CONFIG_INVALID``."""

    code = "CONFIG_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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
                "the encrypted secret store requires OFT_MASTER_KEY to be set"
            )
        if len(master) < MIN_MASTER_KEY_LENGTH:
            raise SecretStoreUnavailable(
                f"OFT_MASTER_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters"
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
                    "the encrypted secret store has an unknown format version"
                )
            salt = base64.b64decode(envelope["salt"])
            token = envelope["token"].encode("ascii")
            plaintext = Fernet(_derive_fernet_key(master, salt)).decrypt(token)
            values = json.loads(plaintext.decode("utf-8"))
        except SecretStoreUnavailable:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidToken) as exc:
            raise SecretStoreUnavailable(
                "the encrypted secret store cannot be decrypted with this master key"
            ) from exc
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise SecretStoreUnavailable("the encrypted secret store is corrupt")
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


def default_backend(settings: Settings) -> SecretBackend | None:
    """The OS keyring first, then the encrypted file, else ``None``.

    Tests and the composition root may inject a backend explicitly; this
    function is the single production selection point. A weak
    ``OFT_MASTER_KEY`` is a startup error, never a silent downgrade.
    """
    keyring_backend = KeyringSecretBackend()
    if keyring_backend.available():
        return keyring_backend
    file_backend = EncryptedFileSecretBackend(data_dir=settings.data_dir)
    master = file_backend._master()  # noqa: SLF001 - the selection probe
    if master is not None and len(master) < MIN_MASTER_KEY_LENGTH:
        raise SecretStoreUnavailable(
            f"OFT_MASTER_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters"
        )
    if master is None and file_backend.path.exists():
        LOGGER.warning(
            "secret_store_locked_out",
            reason="secrets.enc exists but OFT_MASTER_KEY is not set",
        )
    if file_backend.available():
        return file_backend
    return None


@dataclass(frozen=True)
class SecretStatus:
    """The only key-shaped data a surface may render."""

    provider: str
    configured: bool
    key_source: str
    masked: str | None
    fingerprint: str | None
    updated_at: str | None
    updated_by: str | None

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


class SecretsService:
    """Write, rotate, and delete provider keys; never reveal one."""

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
        self._backend = (
            backend
            if backend is not None
            else (default_backend(settings) if settings is not None else None)
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def reference(provider: str) -> str:
        return f"llm.{provider}.{SECRET_NAME}"

    def _env_key(self, provider: str) -> str | None:
        attribute = PROVIDER_KEY_FIELDS.get(provider)
        if attribute is None:
            return None
        secret = getattr(self._settings, attribute, None)
        getter = getattr(secret, "get_secret_value", None)
        value = getter() if callable(getter) else secret
        return value if isinstance(value, str) and value.strip() else None

    def _stored_value(self, provider: str) -> str | None:
        backend = self._backend
        if backend is None or not backend.available():
            return None
        try:
            value = backend.get(self.reference(provider))
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            LOGGER.error("secret_store_read_failed", provider=provider)
            raise SecretStoreUnavailable("the secure secret store is unavailable") from exc
        return value if isinstance(value, str) and value else None

    def _stored_or_unavailable(self, provider: str, meta: SecretMetaRecord | None) -> str | None:
        """Read a stored value; raise when metadata says one should exist.

        A metadata row plus an unavailable/undecryptable backend is a distinct
        state, not "no key": returning ``None`` would silently fall back to an
        environment key the operator does not have in effect.
        """
        if meta is None:
            return None
        backend = self._backend
        if backend is None or not backend.available():
            raise SecretStoreUnavailable(
                "the stored provider key exists but the secret store is unavailable"
            )
        return self._stored_value(provider)

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
        if backend is None or not backend.available():
            raise SecretStoreUnavailable(
                "no secure secret store is available; install an OS keyring backend "
                "or set OFT_MASTER_KEY for the encrypted-file store"
            )
        return backend

    # -- reads -----------------------------------------------------------

    def status(self, provider: str) -> SecretStatus:
        name = validate_provider(provider) or ""
        meta = self._store.get_secret_meta(name, SECRET_NAME)
        try:
            stored = self._stored_or_unavailable(name, meta)
        except SecretStoreUnavailable:
            LOGGER.error("secret_store_unavailable", provider=name)
            return SecretStatus(
                provider=name,
                configured=False,
                key_source="unavailable",
                masked=None,
                fingerprint=meta.fingerprint if meta is not None else None,
                updated_at=meta.updated_at if meta is not None else None,
                updated_by=meta.updated_by if meta is not None else None,
            )
        if meta is not None and stored is not None:
            backend = self._backend
            return SecretStatus(
                provider=name,
                configured=True,
                key_source=backend.kind if backend is not None else "secret",
                masked=mask_secret(stored),
                fingerprint=meta.fingerprint,
                updated_at=meta.updated_at,
                updated_by=meta.updated_by,
            )
        if meta is not None and stored is None:
            LOGGER.warning("secret_store_value_missing", provider=name)
        env_key = self._env_key(name)
        if env_key is not None:
            return SecretStatus(
                provider=name,
                configured=True,
                key_source="env",
                masked=None,
                fingerprint=None,
                updated_at=None,
                updated_by=None,
            )
        return SecretStatus(
            provider=name,
            configured=False,
            key_source="unset",
            masked=None,
            fingerprint=None,
            updated_at=None,
            updated_by=None,
        )

    def list_statuses(self) -> tuple[SecretStatus, ...]:
        return tuple(self.status(provider) for provider in PROVIDERS)

    def resolve_key(self, provider: str) -> str | None:
        """The stored key (preferred) or the provider's environment key.

        Never consults another provider: deleting the selected provider's key
        surfaces the stable configuration error instead of a silent fallback.
        """
        name = (provider or "").strip().lower()
        if name not in PROVIDERS:
            return None
        meta = self._store.get_secret_meta(name, SECRET_NAME)
        stored = self._stored_or_unavailable(name, meta)
        if stored is not None:
            return stored
        return self._env_key(name)

    def redaction_values(self) -> tuple[str, ...]:
        """In-process values that provider error text must never contain."""
        values: list[str] = []
        for provider in PROVIDERS:
            stored = self._stored_value(provider)
            if stored:
                values.append(stored)
            env_key = self._env_key(provider)
            if env_key:
                values.append(env_key)
        return tuple(values)

    # -- writes ----------------------------------------------------------

    def set_key(self, provider: str, value: object, *, actor: str) -> SecretStatus:
        with self._lifecycle_guard():
            return self._set_key_locked(provider, value, actor=actor)

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
        backend = self._require_backend()
        reference = self.reference(name)
        previous = self._stored_value(name)
        try:
            backend.set(reference, normalized)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            raise SecretStoreUnavailable("the secure secret store rejected the write") from exc

        timestamp = utc_timestamp(self._clock.now())
        digest = fingerprint_secret(normalized)
        suffix = last4(normalized)

        def _build(current: SecretMetaRecord | None) -> SecretChangePlan:
            event_type = SECRET_ROTATED_EVENT if current is not None else SECRET_ADDED_EVENT
            record = SecretMetaRecord(
                provider=name,
                name=SECRET_NAME,
                fingerprint=digest,
                last4=suffix,
                updated_at=timestamp,
                updated_by=actor,
            )
            audit = AuditEventDraft(
                event_type=event_type,
                actor={"kind": "USER", "id": actor},
                subject={"kind": SECRET_SUBJECT_KIND, "id": f"{name}.{SECRET_NAME}"},
                # Only non-secret metadata: a hash, provider, and name.
                payload={
                    "provider": name,
                    "name": SECRET_NAME,
                    "fingerprint": digest,
                    "source": backend.kind,
                },
                created_at=timestamp,
            )
            return SecretChangePlan(record=record, audit=audit)

        try:
            self._store.save_secret_change(name, SECRET_NAME, _build)
        except BaseException:
            # Never leave the backend ahead of the metadata after a DB failure,
            # but never clobber a value a newer write already installed.
            try:
                current = backend.get(reference)
            except Exception:  # noqa: BLE001 - the original failure wins
                current = None
                LOGGER.error("secret_backend_rollback_skipped", provider=name)
            if current == normalized:
                try:
                    if previous is None:
                        backend.delete(reference)
                    else:
                        backend.set(reference, previous)
                except Exception:  # noqa: BLE001 - the original failure wins
                    LOGGER.error("secret_backend_rollback_failed", provider=name)
            raise
        LOGGER.info("secret_stored", provider=name, rotated=previous is not None)
        return self.status(name)

    def delete_key(self, provider: str, *, actor: str) -> SecretStatus:
        with self._lifecycle_guard():
            return self._delete_key_locked(provider, actor=actor)

    def _delete_key_locked(self, provider: str, *, actor: str) -> SecretStatus:
        name = validate_provider(provider) or ""
        meta = self._store.get_secret_meta(name, SECRET_NAME)
        backend = self._backend
        if meta is None:
            # A crash between the backend write and the metadata commit (or a
            # restored database) can leave an orphaned backend value. Revoke it
            # anyway; it is a harmless no-op when absent.
            if backend is not None and backend.available():
                try:
                    backend.delete(self.reference(name))
                except SecretStoreUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - never include the value
                    raise SecretStoreUnavailable(
                        "the secure secret store rejected the delete"
                    ) from exc
            return self.status(name)
        backend = self._require_backend()
        reference = self.reference(name)
        previous = self._stored_value(name)
        try:
            backend.delete(reference)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - never include the value
            raise SecretStoreUnavailable("the secure secret store rejected the delete") from exc

        timestamp = utc_timestamp(self._clock.now())

        def _build(current: SecretMetaRecord | None) -> SecretChangePlan | None:
            if current is None:
                return None
            audit = AuditEventDraft(
                event_type=SECRET_DELETED_EVENT,
                actor={"kind": "USER", "id": actor},
                subject={"kind": SECRET_SUBJECT_KIND, "id": f"{name}.{SECRET_NAME}"},
                payload={"provider": name, "name": SECRET_NAME},
                created_at=timestamp,
            )
            return SecretChangePlan(record=None, audit=audit)

        try:
            self._store.save_secret_change(name, SECRET_NAME, _build)
        except BaseException:
            # Restore only when the delete is still the newest backend state.
            try:
                current = backend.get(reference)
            except Exception:  # noqa: BLE001 - the original failure wins
                current = None
            if current is None and previous is not None:
                try:
                    backend.set(reference, previous)
                except Exception:  # noqa: BLE001 - the original failure wins
                    LOGGER.error("secret_backend_rollback_failed", provider=name)
            raise
        LOGGER.info("secret_deleted", provider=name)
        return self.status(name)


__all__ = [
    "ENCRYPTED_FILE_NAME",
    "EncryptedFileSecretBackend",
    "MASTER_KEY_ENV",
    "SecretBackend",
    "SecretStatus",
    "SecretStoreUnavailable",
    "SecretsService",
    "default_backend",
]
