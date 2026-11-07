"""Security negatives: no provider/exchange/Telegram path, no credential needed."""

from __future__ import annotations

import ast
from pathlib import Path

from oft.adapters.llm_stub import LLMStub
from oft.ports.llm import LLMRequest, LLMRole

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "oft"
_FORBIDDEN_IMPORTS = {
    "openai",
    "anthropic",
    "ccxt",
    "binance",
    "telegram",
    "litellm",
    "requests",
    "httpx",
}
_NEW_MODULES = (
    "adapters/llm_stub.py",
    "application/analysis_service.py",
    "application/entry_service.py",
    "application/execution_gateway.py",
    "application/kill_switch.py",
    "application/llm_validation.py",
    "application/opportunity_service.py",
    "application/paper_broker.py",
    "application/trigger_service.py",
    "domain/llm_output.py",
    "domain/oms.py",
    "domain/opportunities.py",
    "domain/risk.py",
    "domain/trigger.py",
    "ports/llm.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_new_module_imports_a_provider_exchange_or_telegram_sdk() -> None:
    violations: list[str] = []
    for relative in _NEW_MODULES:
        found = _imports(SRC / relative) & _FORBIDDEN_IMPORTS
        if found:
            violations.append(f"{relative}: {sorted(found)}")

    assert not violations, "\n".join(violations)


def test_stub_provider_works_without_any_credential(monkeypatch) -> None:
    for name in (
        "OFT_OPENAI_API_KEY",
        "OFT_ANTHROPIC_API_KEY",
        "OFT_DEEPSEEK_API_KEY",
        "OFT_BINANCE_API_KEY",
        "OFT_BINANCE_API_SECRET",
        "OFT_TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    stub = LLMStub()

    result = stub.complete(
        LLMRequest(
            role=LLMRole.ANALYSIS,
            model="fixture",
            system_prompt="system",
            user_prompt="user",
            schema_json="{}",
            input_hash="abc",
            prompt_id="analysis",
            prompt_version=1,
            prompt_hash="hash",
        )
    )

    assert result.provider == "stub"
    assert result.model == "fixture"
    assert result.raw_text
    assert stub.estimate_cost(None, "fixture").unpriced is True  # type: ignore[arg-type]
