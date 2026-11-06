"""Local API authentication and browser CSRF/origin guards (ADR-0003/0010)."""

from __future__ import annotations

import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import Request

from oft.config import Settings

CSRF_COOKIE = "oft_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_COOKIE = "oft_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
TOKEN_FILE = Path("run") / "api-token"
_BEARER_PREFIX = "bearer "


class SessionStore:
    """In-memory browser sessions; a daemon restart invalidates every session."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = time.monotonic() + self._ttl
        return session_id

    def is_valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        expires_at = self._sessions.get(session_id)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._sessions.pop(session_id, None)
            return False
        return True


def _token_path(settings: Settings) -> Path:
    return settings.data_dir / TOKEN_FILE


def ensure_api_token(settings: Settings) -> str:
    """Return the daemon token, creating a 0600 file on first start.

    An explicit ``OFT_API_TOKEN`` (``Settings.api_token``) always wins and is
    never written to disk.
    """
    if settings.api_token is not None:
        return settings.api_token.get_secret_value()

    path = _token_path(settings)
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def read_api_token(settings: Settings) -> str | None:
    """Read the token from settings or the run directory without creating it."""
    if settings.api_token is not None:
        return settings.api_token.get_secret_value()
    path = _token_path(settings)
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def extract_bearer(header: str | None) -> str | None:
    if header is None:
        return None
    lowered = header.lower()
    if not lowered.startswith(_BEARER_PREFIX):
        return None
    token = header[len(_BEARER_PREFIX) :].strip()
    return token or None


def token_matches(expected: str, presented: str) -> bool:
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def _origin(host: str, port: int) -> str:
    """Bracket IPv6 literals so the origin is a valid URL (``http://[::1]:port``)."""
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def allowed_origins(settings: Settings) -> frozenset[str]:
    hosts = {settings.host}
    if settings.host in {"127.0.0.1", "localhost", "::1"}:
        hosts.update({"127.0.0.1", "localhost", "::1"})
    return frozenset(_origin(host, settings.port) for host in hosts)


def is_browser_request(request: Request) -> bool:
    if request.headers.get("origin") is not None:
        return True
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site in {"same-origin", "same-site", "cross-site"}


def csrf_matches(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header:
        return False
    return hmac.compare_digest(cookie.encode("utf-8"), header.encode("utf-8"))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
