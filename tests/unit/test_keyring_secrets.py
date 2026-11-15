"""OS-keyring delete semantics: a delete failure is not proof of absence (M8)."""

from __future__ import annotations

import pytest

from oft.adapters import keyring_secrets as module
from oft.adapters.keyring_secrets import KeyringSecretBackend, KeyringUnavailable


class _FakeKeyring:
    def __init__(self, remaining: str | None, *, read_error: Exception | None = None) -> None:
        self._remaining = remaining
        self._read_error = read_error
        self.reads = 0

    def get_password(self, _service: str, _reference: str) -> str | None:
        self.reads += 1
        if self._read_error is not None:
            raise self._read_error
        return self._remaining

    def set_password(self, _service: str, _reference: str, _value: str) -> None:
        return None

    def delete_password(self, _service: str, _reference: str) -> None:
        raise module._PasswordDeleteError("cannot delete")


@pytest.fixture()
def _available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KeyringSecretBackend, "available", lambda self: True)


def test_a_confirmed_absent_value_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, _available: None
) -> None:
    """M-L: absence is confirmed by exactly one read-back after the failure."""
    fake = _FakeKeyring(None)
    monkeypatch.setattr(module, "_keyring", fake)
    KeyringSecretBackend().delete("llm.openai.api_key")
    assert fake.reads == 1


def test_a_delete_error_with_a_remaining_value_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, _available: None
) -> None:
    monkeypatch.setattr(module, "_keyring", _FakeKeyring("sk-still-there-4f2a"))
    with pytest.raises(KeyringUnavailable):
        KeyringSecretBackend().delete("llm.openai.api_key")


def test_a_delete_error_with_a_read_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, _available: None
) -> None:
    read_error = module._KeyringError("read refused")
    monkeypatch.setattr(module, "_keyring", _FakeKeyring(None, read_error=read_error))
    with pytest.raises(KeyringUnavailable):
        KeyringSecretBackend().delete("llm.openai.api_key")
