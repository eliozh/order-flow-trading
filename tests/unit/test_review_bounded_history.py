"""Unit: the review prompt's bounded management history (M13)."""

from __future__ import annotations

import json

from oft.application.llm_service_support import (
    BOUNDED_HISTORY_MAX_BYTES,
    BOUNDED_HISTORY_MAX_ENTRIES,
    BOUNDED_HISTORY_TEXT_LIMIT,
)
from oft.application.review_service import (
    _bounded_history,
)


def test_review_history_is_bounded_for_the_prompt() -> None:
    entries = [
        {
            "decision_id": f"MGD-{index}",
            "action": "HOLD",
            "at": "2026-10-08T00:00:00.000Z",
            "note": f"{index}:" + "x" * 10_000,
            "rationale": "y" * 10_000,
        }
        for index in range(100)
    ]

    bounded = _bounded_history(entries)

    assert 0 < len(bounded) <= BOUNDED_HISTORY_MAX_ENTRIES
    assert len(json.dumps(bounded, sort_keys=True).encode("utf-8")) <= BOUNDED_HISTORY_MAX_BYTES
    assert all(len(entry["note"]) <= BOUNDED_HISTORY_TEXT_LIMIT for entry in bounded)
    assert all(len(entry["rationale"]) <= BOUNDED_HISTORY_TEXT_LIMIT for entry in bounded)
    # The newest entries are retained.
    assert bounded[-1]["decision_id"] == "MGD-99"
    assert bounded[-1]["note"].startswith("99:")


def test_review_history_ignores_malformed_entries() -> None:
    assert _bounded_history(None) == []
    assert _bounded_history({"note": "x"}) == []
    assert _bounded_history([1, "x", {"decision_id": "MGD-1"}]) == [{"decision_id": "MGD-1"}]
