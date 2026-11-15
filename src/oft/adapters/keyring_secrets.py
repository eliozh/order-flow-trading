"""Optional OS-keyring adapter for write-only provider keys (slice 35).

``keyring`` is imported lazily and remains optional: a headless Linux host with
no Secret Service backend must not fail to import the daemon. When the library
is missing or only the ``fail``/``null`` backend is available,
:meth:`KeyringSecretBackend.available` returns ``False`` and the composition
root falls back to the encrypted file (which itself requires
``OFT_MASTER_KEY``) or refuses the write.

No method ever logs, returns for display, or raises with a key value.
"""

from __future__ import annotations

try:  # pragma: no cover - the availability branch owns the fallback behaviour
    import keyring as _keyring
    from keyring.errors import KeyringError as _KeyringError
    from keyring.errors import PasswordDeleteError as _PasswordDeleteError
except Exception:  # noqa: BLE001 - any import-time failure is an unavailable backend
    _keyring = None  # type: ignore[assignment]

    class _KeyringError(Exception):  # type: ignore[no-redef]
        """Placeholder so the adapter can be constructed without keyring."""

    class _PasswordDeleteError(_KeyringError):  # type: ignore[no-redef]
        """Placeholder delete error when keyring is absent."""


SERVICE_NAME = "oft"
# An allowlist, never a denylist: only a known secure OS credential store may
# accept a key. ``keyrings.alt`` (plaintext file), the fail/null stubs, and an
# unverified ``ChainerBackend`` are all refused, so a convenience package can
# never silently downgrade the store to plaintext.
_SECURE_BACKEND_MARKERS = (
    "secretservice",
    "libsecret",
    "kwallet",
    "macos",
    "windows",
    "win32",
    "winvault",
)
_REFUSED_BACKEND_MARKERS = (
    "keyrings.alt",
    "plaintext",
    "fail",
    "null",
    "chainer",
)


class KeyringUnavailable(RuntimeError):
    """The OS keyring is not usable; the caller must fall back or refuse."""


def _backend_identification(backend: object) -> str:
    backend_type = type(backend)
    return f"{backend_type.__module__}.{backend_type.__name__}".lower()


def keyring_available() -> bool:
    """Whether a known secure OS keyring backend is present."""
    if _keyring is None:
        return False
    try:
        backend = _keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any backend probe failure means unavailable
        return False
    identification = _backend_identification(backend)
    if any(marker in identification for marker in _REFUSED_BACKEND_MARKERS):
        return False
    if not any(marker in identification for marker in _SECURE_BACKEND_MARKERS):
        return False
    return callable(getattr(backend, "get_password", None))


class KeyringSecretBackend:
    """Store keys in the OS credential store under the ``oft`` service name."""

    kind = "keyring"

    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self._service_name = service_name

    def available(self) -> bool:
        return keyring_available()

    def get(self, reference: str) -> str | None:
        if not self.available():
            raise KeyringUnavailable("the OS keyring is unavailable")
        try:
            value = _keyring.get_password(self._service_name, reference)
        except _KeyringError as exc:
            raise KeyringUnavailable("the OS keyring rejected the read") from exc
        return value if isinstance(value, str) and value else None

    def set(self, reference: str, value: str) -> None:
        if not self.available():
            raise KeyringUnavailable("the OS keyring is unavailable")
        try:
            _keyring.set_password(self._service_name, reference, value)
        except _KeyringError as exc:
            raise KeyringUnavailable("the OS keyring rejected the write") from exc

    def delete(self, reference: str) -> None:
        if not self.available():
            raise KeyringUnavailable("the OS keyring is unavailable")
        try:
            _keyring.delete_password(self._service_name, reference)
        except _PasswordDeleteError as exc:
            # ``PasswordDeleteError`` is a real delete failure, not proof the
            # value is absent. Only a confirming read makes it a safe no-op;
            # otherwise the caller must not record a false revocation.
            try:
                remaining = _keyring.get_password(self._service_name, reference)
            except _KeyringError as read_exc:
                raise KeyringUnavailable("the OS keyring rejected the delete") from read_exc
            if remaining is None:
                return
            raise KeyringUnavailable("the OS keyring rejected the delete") from exc
        except _KeyringError as exc:
            raise KeyringUnavailable("the OS keyring rejected the delete") from exc


__all__ = ["KeyringSecretBackend", "KeyringUnavailable", "keyring_available"]
