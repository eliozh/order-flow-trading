"""Shared secret-store failure contract.

The application ``SecretsService`` owns the store lifecycle, but a lazy secret
resolver injected into an adapter must distinguish an unavailable backend from
a programming error without importing the application layer. The exception
lives here so both sides share one type.
"""

from __future__ import annotations


class SecretStoreUnavailable(Exception):
    """No secure store could accept the key; surfaced as ``CONFIG_INVALID``.

    ``reason`` is a stable, non-secret token for logs and tests
    (``backend_unavailable``, ``value_missing``, ``decrypt_failed``,
    ``selected_backend_unavailable``); it never contains a key or master key.
    """

    code = "CONFIG_INVALID"

    def __init__(self, message: str, *, reason: str = "backend_unavailable") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


__all__ = ["SecretStoreUnavailable"]
