"""Tolerant decoding of stored contract payloads (one corruption policy).

A single corrupt row must never turn a read model into a 500. Callers that
serve one row convert ``None`` into a typed ``DATA_UNAVAILABLE`` failure; list
read models skip the row and keep serving, with the skip named in a warning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def decode_payload_json(payload_json: str, *, subject: str) -> dict[str, Any] | None:
    """Decode one stored JSON payload, or ``None`` when it is corrupt.

    ``subject`` names the row in the warning so an operator can locate it. The
    decode accepts only a JSON object: a scalar, array, or ``null`` payload is
    as unusable as malformed text and follows the same policy.
    """
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        _LOGGER.warning("skipping corrupt payload for %s", subject)
        return None
    if not isinstance(payload, dict):
        _LOGGER.warning("skipping non-object payload for %s", subject)
        return None
    return payload


__all__ = ["decode_payload_json"]
