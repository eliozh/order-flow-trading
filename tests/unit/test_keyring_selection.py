"""Only a known secure OS keyring backend may accept a provider key (M2)."""

from __future__ import annotations

from typing import Any

import pytest

from oft.adapters import keyring_secrets


class _Backend:
    def get_password(self, service: str, name: str) -> str | None:  # pragma: no cover - probe
        return None


def _backend(module: str, name: str) -> Any:
    backend_type = type(name, (_Backend,), {})
    backend_type.__module__ = module
    return backend_type()


class _Keyring:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def get_keyring(self) -> Any:
        return self._backend


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.libsecret", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
    ],
)
def test_known_secure_backends_are_available(
    monkeypatch: pytest.MonkeyPatch, module: str, name: str
) -> None:
    monkeypatch.setattr(keyring_secrets, "_keyring", _Keyring(_backend(module, name)))
    assert keyring_secrets.keyring_available() is True


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("keyrings.alt.file", "PlaintextKeyring"),
        ("keyring.backends.fail", "Keyring"),
        ("keyring.backends.null", "Keyring"),
        ("keyring.backends.chainer", "ChainerBackend"),
        ("some.unknown.backend", "MysteryKeyring"),
    ],
)
def test_insecure_or_unverified_backends_are_refused(
    monkeypatch: pytest.MonkeyPatch, module: str, name: str
) -> None:
    monkeypatch.setattr(keyring_secrets, "_keyring", _Keyring(_backend(module, name)))
    assert keyring_secrets.keyring_available() is False
