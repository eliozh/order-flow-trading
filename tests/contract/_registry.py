"""Local JSON Schema registry: no schema is fetched over the network."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"

# Every record family the deterministic paper loop persists, mapped to its
# contract. Kept here so a new family cannot be added without registering it.
PAPER_LOOP_CONTRACTS: dict[str, str] = {
    "budget": "budget-record.schema.json",
    "decision": "llm-decision.schema.json",
    "fill": "fill.schema.json",
    "order": "order.schema.json",
    "position": "position.schema.json",
    "trigger": "trigger-event.schema.json",
}


def load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def registry() -> Registry:
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    built: Registry = Registry().with_resources(resources)
    return built


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(name), registry=registry())


def validate(name: str, payload: dict[str, Any]) -> None:
    errors = sorted(validator(name).iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)
