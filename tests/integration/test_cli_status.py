"""Walking-skeleton integration test: CLI -> domain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oft.bootstrap import get_status
from oft.cli.main import app
from oft.config import ConfigError, Settings, load_settings


def _configure_cli(monkeypatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def test_cli_status_json_uses_shared_domain_path(monkeypatch, tmp_path: Path) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    expected = get_status(settings)

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["data"]["universe"] == expected.universe.model_dump(mode="json")
    assert payload["data"]["env"] == expected.env
    assert payload["meta"]["provenance"] == "CALC"


def test_cli_status_table_is_stable(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    output = result.stdout
    assert "Environment" in output
    assert "PAPER" not in output
    assert "test" in output
    assert "BTCUSDT" in output
    assert "TRADIFI_PERPETUAL" in output


def test_cli_and_domain_share_the_same_symbols(monkeypatch, tmp_path: Path) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    domain = get_status(settings)

    result = CliRunner().invoke(app, ["status", "--json"])

    cli = json.loads(result.stdout)["data"]
    assert [item["symbol"] for item in cli["universe"]["symbols"]] == [
        item.symbol for item in domain.universe.symbols
    ]
    assert cli["universe"]["count"] == domain.universe.count


def _combined_output(result) -> str:
    return result.output + (getattr(result, "stderr", "") or "")


def test_serve_rejects_non_loopback_host_without_override(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 5, result.output
    assert "non-loopback" in _combined_output(result).lower()


def test_serve_warns_and_runs_with_allow_non_loopback(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))

    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--allow-non-loopback"])

    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"
    assert "warning" in _combined_output(result).lower()
    assert "non-loopback" in _combined_output(result).lower()


def test_serve_applies_cli_port_to_the_app_settings(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    import uvicorn

    from oft.api.auth import allowed_origins

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["app"] = args[0] if args else kwargs.get("app")
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(app, ["serve", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert captured["port"] == 9999
    assert captured["host"] == "127.0.0.1"
    assert captured["access_log"] is False
    factory = captured["app"]
    assert callable(factory)
    resolved_app = factory()
    assert resolved_app.state.settings.port == 9999
    assert "http://127.0.0.1:9999" in allowed_origins(resolved_app.state.settings)


def test_allowed_origins_brackets_ipv6_loopback() -> None:
    from oft.api.auth import allowed_origins

    origins = allowed_origins(Settings(env="test", host="::1", port=7612))

    assert "http://[::1]:7612" in origins


def test_open_prints_the_bootstrap_url(monkeypatch, tmp_path: Path) -> None:
    from oft.api.auth import write_browser_bootstrap

    settings = _configure_cli(monkeypatch, tmp_path)
    secret = write_browser_bootstrap(settings)

    result = CliRunner().invoke(app, ["open", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "?bootstrap=" in result.stdout
    assert secret in result.stdout
    assert f"127.0.0.1:{settings.port}" in result.stdout


def test_open_without_a_daemon_reports_an_error(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["open", "--no-open"])

    assert result.exit_code == 1
    assert "oft serve" in _combined_output(result)


def test_an_unknown_env_provider_is_config_invalid_without_a_traceback(
    monkeypatch, tmp_path: Path
) -> None:
    """H2: an unknown OFT_LLM_PROVIDER is typed at load and in the CLI."""
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_LLM_PROVIDER", "bogus")

    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.code == "CONFIG_INVALID"

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert "traceback" not in _combined_output(result).lower()


def test_refresh_converts_a_setting_validation_error_to_config_invalid(
    monkeypatch, tmp_path: Path
) -> None:
    """H2: the refresh path never leaks an untyped validation error."""
    _configure_cli(monkeypatch, tmp_path)
    import oft.bootstrap as bootstrap_module
    from oft.domain.settings import SettingValidationError

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = bootstrap_module.build_container(settings)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise SettingValidationError("bad provider")

    monkeypatch.setattr(bootstrap_module, "resolve_effective_llm_settings", _raise)
    with pytest.raises(ConfigError) as caught:
        container.refresh_llm_settings()
    assert caught.value.code == "CONFIG_INVALID"


def test_the_inner_env_provider_conversion_is_a_config_error(tmp_path: Path) -> None:
    """M-N: ``_env_provider`` itself converts a validation failure."""
    import oft.bootstrap as bootstrap_module

    settings = Settings.model_construct(llm_provider="bogus", data_dir=tmp_path / "data")
    with pytest.raises(ConfigError) as caught:
        bootstrap_module.build_container(settings)
    assert caught.value.code == "CONFIG_INVALID"
    assert "OFT_LLM_PROVIDER" in caught.value.message


def test_a_short_master_key_without_a_keyring_still_runs_status(
    monkeypatch, tmp_path: Path
) -> None:
    """H3: selection never raises during composition; status still works."""
    from oft.adapters import keyring_secrets as keyring_module

    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_MASTER_KEY", "too-short")
    monkeypatch.setattr(keyring_module, "keyring_available", lambda: False)

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert "traceback" not in _combined_output(result).lower()
