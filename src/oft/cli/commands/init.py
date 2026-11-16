"""``oft init``: paper-only, resumable first-run onboarding.

The command runs with the daemon down and drives the same application services
as the Web wizard: :mod:`oft.application.onboarding_service` owns the step
identities and progress document, :class:`SettingsService` persists scan/LLM
settings, :class:`SecretsService` stores provider keys write-only, and
:class:`BudgetService` owns thresholds. No step definition is duplicated here,
and no Binance, Telegram, or Live configuration is offered: steps 3 and 4 are
explicitly deferred to Milestones 5 and 6.

Secrets are never accepted on argv. A key is read from stdin with no echo (a
TTY uses ``getpass``; a pipe reads one line) and is only ever rendered as the
service's fixed mask.
"""

from __future__ import annotations

import getpass
import re
import sys
from dataclasses import dataclass
from typing import Annotated

import typer

from oft.application.budget_read_model import (
    BudgetConfigError,
    BudgetThresholds,
    candidate_thresholds,
    format_text,
)
from oft.application.budget_service import BudgetService
from oft.application.onboarding_service import (
    ACTIVE_ONBOARDING_STEPS,
    ONBOARDING_STEPS,
)
from oft.application.secrets_service import (
    SecretsService,
    SecretStatus,
    SecretStoreUnavailable,
)
from oft.application.settings_service import SettingsService
from oft.application.symbol_registry import SymbolRegistry
from oft.bootstrap import AppContainer, build_container
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, Settings, load_settings
from oft.domain.llm_settings import MAX_API_KEY_LENGTH, PROVIDERS, validate_provider
from oft.domain.settings import SettingValidationError, validate_scan_period
from oft.domain.time import utc_timestamp

# Steps 3 (Binance) and 4 (Telegram) are deferred by the shared definition.
_DEFERRED_STEPS = {step.step: step.deferred_milestone for step in ONBOARDING_STEPS if step.deferred}
_STEP_TITLES = {step.step: step.title for step in ONBOARDING_STEPS}
_BUDGET_FIELDS = ("daily_usd", "weekly_usd", "monthly_usd")
# A threshold is a plain USD amount. Reject exponent notation and unbounded
# precision locally, before the shared writer's ``format(value, "f")`` can
# build an enormous string after the watchlist/scan settings were written.
_BUDGET_MAX_INTEGER_DIGITS = 9  # 999,999,999 USD
_BUDGET_MAX_FRACTION_DIGITS = 8
_BUDGET_PATTERN = re.compile(
    rf"^(?:0|[1-9]\d{{0,{_BUDGET_MAX_INTEGER_DIGITS - 1}}})"
    rf"(?:\.\d{{1,{_BUDGET_MAX_FRACTION_DIGITS}}})?$"
)


class _MissingFlags(Exception):
    """One or more required non-interactive flags were omitted."""

    def __init__(self, flags: list[str]) -> None:
        super().__init__(", ".join(flags))
        self.flags = flags


@dataclass(frozen=True)
class _InitOptions:
    """The raw flag values, threaded through one object instead of nine args."""

    resume: bool = False
    non_interactive: bool = False
    llm_provider: str | None = None
    llm_key_stdin: bool = False
    watchlist: str | None = None
    scan_period: str | None = None
    budget_daily: str | None = None
    budget_weekly: str | None = None
    budget_monthly: str | None = None


@dataclass(frozen=True)
class _NonInteractivePlan:
    """Validated inputs for the incomplete active steps; nothing written yet."""

    incomplete: tuple[int, ...]
    provider: str | None
    key_stdin: bool
    symbols: tuple[str, ...]
    period: str | None
    budget: dict[str, str] | None


# -- small seams (monkeypatched by tests) ------------------------------------


def _stdin_is_tty() -> bool:
    """Whether interactive prompting is possible; tests patch this seam."""
    return sys.stdin.isatty()


def _prompt_text(label: str, *, default: str = "") -> str:
    """One visible prompt; tests patch this seam to simulate an interrupt."""
    return str(typer.prompt(label, default=default, show_default=bool(default)))


def _read_secret_key() -> str:
    """Read one key from stdin without echoing it; never from argv.

    A TTY uses ``getpass``; a pipe reads at most one bounded line. An abort at
    the hidden prompt (Ctrl-D raises ``EOFError``) becomes the same resumable
    interruption as Ctrl-C.
    """
    try:
        if _stdin_is_tty():
            return getpass.getpass("Provider API key (input hidden): ")
        raw = sys.stdin.readline(MAX_API_KEY_LENGTH + 2)
    except (EOFError, OSError):
        raise typer.Abort() from None
    value = raw.rstrip("\r\n")
    if len(value) > MAX_API_KEY_LENGTH:
        message = f"api_key must be at most {MAX_API_KEY_LENGTH} non-space characters"
        raise SettingValidationError(
            message,
            details=[{"field": "api_key", "code": "too_long", "message": message}],
        )
    return value


# -- rendering ---------------------------------------------------------------


def _key_label(status: SecretStatus) -> str:
    """The only key-shaped text any init surface may print."""
    return status.masked or status.key_source or "unset"


def _print_environment(settings: Settings) -> None:
    typer.echo(f"1/5 {_STEP_TITLES[1]}")
    typer.echo(f"  data directory  {settings.data_dir}")
    typer.echo(f"  bind            {settings.host}:{settings.port}")
    typer.echo("  environment     PAPER (Live is not offered by `oft init`)")


def _print_providers(secrets: SecretsService) -> None:
    typer.echo(f"2/5 {_STEP_TITLES[2]}")
    for status in secrets.list_statuses():
        typer.echo(f"  {status.provider:<10} {_key_label(status)}")


def _print_deferred_steps() -> None:
    binance = _DEFERRED_STEPS[3]
    telegram = _DEFERRED_STEPS[4]
    typer.echo(
        f"3/5 {_STEP_TITLES[3]}: deferred to Milestone {binance}; "
        "init never prompts for Binance tokens."
    )
    typer.echo(
        f"4/5 {_STEP_TITLES[4]}: deferred to Milestone {telegram}; "
        "init never prompts for a bot token or chat id."
    )


def _budget_source(container: AppContainer) -> str:
    return "user" if container.store.get_budget_settings() is not None else "default"


def _print_watchlist_state(
    container: AppContainer,
    service: SettingsService,
    budget_service: BudgetService,
) -> None:
    registry = container.symbol_registry
    starred = tuple(contract.symbol for contract in registry.list_starred())
    counts = registry.group_counts()
    scan = service.read_scan_settings()
    thresholds = budget_service.thresholds()
    typer.echo(f"5/5 {_STEP_TITLES[5]}")
    typer.echo(f"  registry  {counts['crypto']} crypto / {counts['tradfi']} tradfi")
    typer.echo(f"  starred   {', '.join(starred) if starred else 'none'}")
    typer.echo(f"  scan.period {scan.value} (source {scan.source.value})")
    typer.echo(
        "  budget    day "
        f"{format_text(thresholds.daily_usd)} / week {format_text(thresholds.weekly_usd)} / "
        f"month {format_text(thresholds.monthly_usd)} USD "
        f"(source {_budget_source(container)})"
    )


def _print_summary(
    container: AppContainer,
    service: SettingsService,
    secrets: SecretsService,
    budget_service: BudgetService,
) -> None:
    progress = service.read_onboarding_progress()
    completed = ", ".join(str(step) for step in progress.completed_steps) or "none"
    typer.echo("")
    typer.echo(f"Completed steps: {completed}")
    typer.echo("LLM keys:")
    for status in secrets.list_statuses():
        typer.echo(f"  {status.provider:<10} {_key_label(status)}")
    starred = container.symbol_registry.list_starred()
    crypto = sum(1 for contract in starred if not contract.tradifi)
    tradfi = len(starred) - crypto
    typer.echo(f"Watchlist: {len(starred)} starred ({crypto} crypto / {tradfi} tradfi)")
    scan = service.read_scan_settings()
    typer.echo(f"Scan: {scan.value} (source {scan.source.value}); next run {scan.next_run}")
    thresholds = budget_service.thresholds()
    typer.echo(
        "Budget: day "
        f"{format_text(thresholds.daily_usd)} / week {format_text(thresholds.weekly_usd)} / "
        f"month {format_text(thresholds.monthly_usd)} USD "
        f"(source {_budget_source(container)})"
    )
    typer.echo("Environment: PAPER. Live is not offered by `oft init`.")


# -- validation helpers ------------------------------------------------------


def _parse_symbols(raw: str | None, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse a comma-separated watchlist; blank input falls back to ``default``."""
    text = (raw or "").strip()
    if not text:
        if not default:
            message = "watchlist must name at least one symbol"
            raise SettingValidationError(
                message,
                details=[{"field": "watchlist", "code": "empty", "message": message}],
            )
        return default
    tokens = [item.strip().upper() for item in text.split(",")]
    if any(not token for token in tokens):
        message = "watchlist contains a blank symbol"
        raise SettingValidationError(
            message,
            details=[{"field": "watchlist", "code": "blank_symbol", "message": message}],
        )
    # Preserve order while de-duplicating.
    return tuple(dict.fromkeys(tokens))


def _validate_symbols(registry: SymbolRegistry, symbols: tuple[str, ...]) -> None:
    unknown = [symbol for symbol in symbols if registry.get_symbol(symbol) is None]
    if unknown:
        message = (
            f"unknown watchlist symbol(s): {', '.join(unknown)}; "
            "star only symbols that exist in the registry"
        )
        raise SettingValidationError(
            message,
            details=[
                {"field": "watchlist", "code": "unknown_symbol", "message": message},
            ],
        )


def _budget_value(value: str, *, field: str) -> str:
    """Reject exponent/huge/precision-abusive USD values before any write.

    The shared budget writer renders thresholds with ``format(value, "f")``, so
    an exponent such as ``1e999999999`` could allocate an unbounded string
    after the watchlist was already starred. Plain bounded decimals keep that
    path safe without changing the shared service.
    """
    text = value.strip()
    if _BUDGET_PATTERN.fullmatch(text) is None:
        message = (
            f"{field} must be a plain USD amount up to 999999999.99999999; "
            "exponent notation is not accepted"
        )
        raise BudgetConfigError(
            message,
            details=[{"field": field, "code": "out_of_range", "message": message}],
        )
    return text


def _budget_overrides(
    budget_service: BudgetService,
    *,
    daily: str | None,
    weekly: str | None,
    monthly: str | None,
) -> dict[str, str] | None:
    """Validate optional budget values before anything is written.

    Validation matches ``BudgetService.update_thresholds`` exactly, ordering
    included, so a rejected candidate can never leave a star or ``scan.period``
    behind.
    """
    raw_values = {
        field: value
        for field, value in zip(_BUDGET_FIELDS, (daily, weekly, monthly), strict=True)
        if value is not None
    }
    if not raw_values:
        return None
    overrides = {field: _budget_value(value, field=field) for field, value in raw_values.items()}
    current: BudgetThresholds = budget_service.thresholds()
    candidate_thresholds(
        current,
        daily_usd=overrides.get("daily_usd"),
        weekly_usd=overrides.get("weekly_usd"),
        monthly_usd=overrides.get("monthly_usd"),
        warn_pct=None,
    )
    return overrides


def _parse_budget_prompt(raw: str) -> dict[str, str] | None:
    text = (raw or "").strip()
    if not text:
        return None
    parts = [part for part in text.replace(",", " ").split() if part]
    if len(parts) != 3:
        message = "budget expects three values: daily weekly monthly"
        raise SettingValidationError(
            message,
            details=[{"field": "budget", "code": "invalid_count", "message": message}],
        )
    return dict(zip(_BUDGET_FIELDS, parts, strict=True))


def _ensure_active_provider(
    service: SettingsService,
    secrets: SecretsService,
    chosen: str | None,
) -> None:
    """Point the active provider at a configured key without extra writes."""
    if chosen is not None:
        active = (service.read_effective_llm().provider or "").strip().lower()
        if active != chosen:
            service.update_llm_settings({"provider": chosen}, actor="cli", reason="init")
        return
    statuses = {status.provider: status for status in secrets.list_statuses()}
    configured = [name for name, status in statuses.items() if status.configured]
    if not configured:
        return
    active = (service.read_effective_llm().provider or "").strip().lower()
    if active in statuses and statuses[active].configured:
        return
    service.update_llm_settings({"provider": configured[0]}, actor="cli", reason="init")


def _apply_scan_period(service: SettingsService, period: str | None) -> None:
    if period is not None:
        service.update_scan_period(period, actor="cli", reason="init")


def _apply_budget(
    budget_service: BudgetService,
    overrides: dict[str, str] | None,
    *,
    now: str,
) -> None:
    if not overrides:
        return
    try:
        budget_service.update_thresholds(
            daily_usd=overrides.get("daily_usd"),
            weekly_usd=overrides.get("weekly_usd"),
            monthly_usd=overrides.get("monthly_usd"),
            updated_at=now,
            actor="cli",
            reason="init",
        )
    except (ArithmeticError, ValueError) as exc:
        message = "budget value is not a usable USD amount"
        raise BudgetConfigError(
            message,
            details=[{"field": "budget", "code": "invalid_value", "message": message}],
        ) from exc


# -- interactive wizard ------------------------------------------------------


def _step_llm_interactive(service: SettingsService, secrets: SecretsService) -> None:
    _print_providers(secrets)
    statuses = secrets.list_statuses()
    chosen: str | None = None
    if not any(status.configured for status in statuses):
        raw = _prompt_text(f"Provider ({'|'.join(PROVIDERS)})")
        chosen = validate_provider(raw)
        if chosen is None:
            raise SettingValidationError("a provider is required to store a key")
        key = _read_secret_key()
        secrets.set_key(chosen, key, actor="cli")
        typer.echo(f"  {chosen} key stored {_key_label(secrets.status(chosen))}")
    _ensure_active_provider(service, secrets, chosen)
    service.complete_onboarding_step(2, actor="cli")


def _step_watchlist_interactive(
    container: AppContainer,
    service: SettingsService,
    budget_service: BudgetService,
) -> None:
    registry = container.symbol_registry
    _print_watchlist_state(container, service, budget_service)
    existing = tuple(contract.symbol for contract in registry.list_starred())
    default = ",".join(existing)
    label = "Starred symbols (comma-separated)"
    raw_symbols = _prompt_text(label, default=default)
    symbols = _parse_symbols(raw_symbols, default=existing)
    _validate_symbols(registry, symbols)

    scan = service.read_scan_settings()
    raw_period = _prompt_text(
        f"Scan period [{', '.join(scan.supported)}] (blank keeps {scan.value})"
    )
    period = validate_scan_period(raw_period) if raw_period.strip() else None

    thresholds = budget_service.thresholds()
    raw_budget = _prompt_text(
        "Budget daily weekly monthly "
        f"[{format_text(thresholds.daily_usd)} {format_text(thresholds.weekly_usd)} "
        f"{format_text(thresholds.monthly_usd)}] (blank keeps)"
    )
    budget = _parse_budget_prompt(raw_budget)
    if budget is not None:
        budget = _budget_overrides(
            budget_service,
            daily=budget["daily_usd"],
            weekly=budget["weekly_usd"],
            monthly=budget["monthly_usd"],
        )

    # Every input is validated before the first prerequisite write.
    for symbol in symbols:
        registry.set_star_with_audit(symbol, True, actor="cli")
    _apply_scan_period(service, period)
    _apply_budget(budget_service, budget, now=utc_timestamp(container.clock.now()))
    service.complete_onboarding_step(5, actor="cli")


def _run_interactive(
    container: AppContainer,
    service: SettingsService,
    secrets: SecretsService,
    budget_service: BudgetService,
    settings: Settings,
    options: _InitOptions,
) -> None:
    progress = service.read_onboarding_progress()
    if progress.all_complete:
        _print_deferred_steps()
        _print_summary(container, service, secrets, budget_service)
        return
    if options.resume:
        typer.echo(f"Resuming at step {progress.resumed_step}/5; completed steps are persisted.")
    completed = set(progress.completed_steps)
    for step in ACTIVE_ONBOARDING_STEPS:
        if step in completed:
            continue
        if step == 1:
            _print_environment(settings)
            service.complete_onboarding_step(1, actor="cli")
        elif step == 2:
            _step_llm_interactive(service, secrets)
        elif step == 5:
            _step_watchlist_interactive(container, service, budget_service)
    _print_deferred_steps()
    _print_summary(container, service, secrets, budget_service)


# -- non-interactive path ----------------------------------------------------


def _plan_non_interactive(
    container: AppContainer,
    service: SettingsService,
    secrets: SecretsService,
    budget_service: BudgetService,
    options: _InitOptions,
) -> _NonInteractivePlan:
    progress = service.read_onboarding_progress()
    completed = set(progress.completed_steps)
    incomplete = tuple(step for step in ACTIVE_ONBOARDING_STEPS if step not in completed)
    registry = container.symbol_registry
    provider: str | None = None
    symbols: tuple[str, ...] = ()
    period: str | None = None
    budget: dict[str, str] | None = None
    missing: list[str] = []

    if 2 in incomplete:
        provider = validate_provider(options.llm_provider)
        if provider is None:
            missing.append("--llm-provider")
        elif not secrets.status(provider).configured and not options.llm_key_stdin:
            missing.append("--llm-key-stdin")

    if 5 in incomplete:
        has_star = container.store.watchlist_snapshot(limit=1).starred_count >= 1
        if options.watchlist is None and not has_star:
            missing.append("--watchlist")
        if options.watchlist is not None:
            symbols = _parse_symbols(options.watchlist)
            _validate_symbols(registry, symbols)
        if options.scan_period is not None:
            period = validate_scan_period(options.scan_period)
        budget = _budget_overrides(
            budget_service,
            daily=options.budget_daily,
            weekly=options.budget_weekly,
            monthly=options.budget_monthly,
        )

    if missing:
        raise _MissingFlags(missing)
    return _NonInteractivePlan(
        incomplete=incomplete,
        provider=provider,
        key_stdin=options.llm_key_stdin,
        symbols=symbols,
        period=period,
        budget=budget,
    )


def _run_non_interactive(
    container: AppContainer,
    service: SettingsService,
    secrets: SecretsService,
    budget_service: BudgetService,
    settings: Settings,
    options: _InitOptions,
) -> None:
    progress = service.read_onboarding_progress()
    if progress.all_complete:
        _print_deferred_steps()
        _print_summary(container, service, secrets, budget_service)
        return

    plan = _plan_non_interactive(container, service, secrets, budget_service, options)
    if options.resume:
        typer.echo(f"Resuming at step {progress.resumed_step}/5; completed steps are persisted.")
    _print_environment(settings)
    _print_providers(secrets)
    _print_deferred_steps()
    _print_watchlist_state(container, service, budget_service)

    # Validation finished above; now apply prerequisites, then progress.
    if 2 in plan.incomplete:
        provider = plan.provider
        if provider is None:  # planning guarantees this; never use assert for control flow
            raise _MissingFlags(["--llm-provider"])
        if plan.key_stdin:
            key = _read_secret_key()
            secrets.set_key(provider, key, actor="cli")
            typer.echo(f"  {provider} key stored {_key_label(secrets.status(provider))}")
        _ensure_active_provider(service, secrets, provider)
    if 5 in plan.incomplete:
        for symbol in plan.symbols:
            container.symbol_registry.set_star_with_audit(symbol, True, actor="cli")
        _apply_scan_period(service, plan.period)
        _apply_budget(budget_service, plan.budget, now=utc_timestamp(container.clock.now()))
    for step in plan.incomplete:
        service.complete_onboarding_step(step, actor="cli")
    _print_summary(container, service, secrets, budget_service)


# -- entry point -------------------------------------------------------------


def _run(options: _InitOptions) -> None:
    if not options.non_interactive and not _stdin_is_tty():
        emit_error(
            "VALIDATION_FAILED",
            "stdin is not a TTY; pass --non-interactive (or run `oft init` in an "
            "interactive terminal).",
            json_output=False,
        )
        raise typer.Exit(code=2)

    settings = load_settings()
    container = build_container(settings, initialize=True)
    base = container.llm_base_settings or settings
    service = SettingsService(store=container.store, clock=container.clock, settings=base)
    secrets = SecretsService(store=container.store, settings=base, clock=container.clock)
    budget_service = BudgetService(store=container.store, settings=container.settings)

    if options.non_interactive:
        _run_non_interactive(
            container,
            service,
            secrets,
            budget_service,
            settings,
            options,
        )
        return
    _run_interactive(
        container,
        service,
        secrets,
        budget_service,
        settings,
        options,
    )


def init_command(
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue at the first incomplete step.")
    ] = False,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Run without prompts; require flags for every incomplete step.",
        ),
    ] = False,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="openai|anthropic|deepseek; required when step 2 is incomplete.",
        ),
    ] = None,
    llm_key_stdin: Annotated[
        bool,
        typer.Option(
            "--llm-key-stdin",
            help="Read the provider key from stdin; a key is never accepted on argv.",
        ),
    ] = False,
    watchlist: Annotated[
        str | None,
        typer.Option(
            "--watchlist",
            help="SYMBOL[,SYMBOL...] to star; required when no symbol is starred.",
        ),
    ] = None,
    scan_period: Annotated[
        str | None, typer.Option("--scan-period", help="Scan period: 4h or 8h.")
    ] = None,
    budget_daily: Annotated[
        str | None, typer.Option("--budget-daily", help="Daily USD threshold.")
    ] = None,
    budget_weekly: Annotated[
        str | None, typer.Option("--budget-weekly", help="Weekly USD threshold.")
    ] = None,
    budget_monthly: Annotated[
        str | None, typer.Option("--budget-monthly", help="Monthly USD threshold.")
    ] = None,
) -> None:
    """Paper-only first-run onboarding; steps 3 and 4 are deferred."""
    options = _InitOptions(
        resume=resume,
        non_interactive=non_interactive,
        llm_provider=llm_provider,
        llm_key_stdin=llm_key_stdin,
        watchlist=watchlist,
        scan_period=scan_period,
        budget_daily=budget_daily,
        budget_weekly=budget_weekly,
        budget_monthly=budget_monthly,
    )
    try:
        _run(options)
    except typer.Exit:
        raise
    except _MissingFlags as exc:
        emit_error(
            "VALIDATION_FAILED",
            "non-interactive init is missing required flag(s): " + ", ".join(exc.flags),
            json_output=False,
        )
        raise typer.Exit(code=2) from exc
    except (typer.Abort, KeyboardInterrupt, EOFError):
        typer.echo("Progress saved. Continue with `oft init --resume`.", err=True)
        raise typer.Exit(code=130) from None
    except SettingValidationError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=False, details=exc.details)
        raise typer.Exit(code=2) from exc
    except BudgetConfigError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=False, details=exc.details)
        raise typer.Exit(code=2) from exc
    except SecretStoreUnavailable as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc


__all__ = ["init_command"]
