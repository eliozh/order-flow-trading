"""``oft init``: paper-only, resumable first-run onboarding.

The command runs with the daemon down and drives the same application services
as the Web wizard: :mod:`oft.application.onboarding_service` owns the step
identities and progress document, :class:`SettingsService` persists scan/LLM/
Telegram settings, :class:`SecretsService` stores provider keys and the bot
token write-only, and :class:`BudgetService` owns thresholds. No step definition
is duplicated here. Step 3 (Binance) stays deferred to its milestone; step 4
(Telegram) is a real **optional** step that can be configured interactively or
through flags, and skipping it leaves pushes disabled without blocking a Paper
finish.

Secrets are never accepted on argv. A provider key or bot token is read from
stdin with no echo (a TTY uses ``getpass``; a pipe reads one line) and is only
ever rendered as the service's fixed mask.

The Telegram prompts, flag parsing, and non-interactive plan live in
:mod:`oft.cli.commands.init_telegram`.

Documented maintainability exception: this module remains above 800 lines
because the interactive wizard's step orchestration, prompt seams, validation
helpers, and error mapping share one entry point and one control flow; the
Telegram half has already been extracted. Splitting further would only move the
same code and duplicate the CLI error boundary. The exception is recorded here
per the repository's soft-ceiling standard and should be revisited if the
wizard grows another step.
"""

from __future__ import annotations

import getpass
import re
import sys
from dataclasses import dataclass, field
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
from oft.application.symbol_registry import SymbolRegistry, SymbolRegistryError
from oft.bootstrap import AppContainer, build_container
from oft.cli.commands.doctor import emit_error
from oft.cli.commands.init_telegram import (
    TelegramOptions,
    TelegramPlan,
    apply_telegram_plan,
    plan_telegram,
    print_telegram_line,
    read_telegram_token,
    step_telegram_interactive,
)
from oft.config import ConfigError, Environment, Settings, load_settings
from oft.domain.events import strip_control_characters
from oft.domain.llm_settings import MAX_API_KEY_LENGTH, PROVIDERS, validate_provider
from oft.domain.settings import SettingValidationError, validate_scan_period
from oft.domain.time import utc_timestamp
from oft.ports.store import StoreUnavailableError

# Step 3 (Binance) is deferred by the shared definition; step 4 (Telegram) is
# active and optional, so it can still be configured after the required steps.
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
# Registry symbols are shaped like ``BTCUSDT``; reject anything else before a
# lookup so a hostile token is never echoed with its control characters.
_WATCHLIST_SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,24}")
_MAX_REPORTED_TOKENS = 10


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
    telegram: TelegramOptions = field(default_factory=TelegramOptions)


@dataclass(frozen=True)
class _NonInteractivePlan:
    """Validated inputs for the incomplete active steps; nothing written yet."""

    incomplete: tuple[int, ...]
    provider: str | None
    key_stdin: bool
    symbols: tuple[str, ...]
    period: str | None
    budget: dict[str, str] | None
    telegram: TelegramPlan = field(default_factory=TelegramPlan)


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


def _read_telegram_token() -> str:
    """Read the bot token from stdin without echo; never from argv."""
    return read_telegram_token(stdin_is_tty=_stdin_is_tty)


# -- rendering ---------------------------------------------------------------


def _key_label(status: SecretStatus) -> str:
    """The only key-shaped text any init surface may print."""
    return status.masked or status.key_source or "unset"


def _print_environment(settings: Settings) -> None:
    typer.echo(f"1/5 {_STEP_TITLES[1]}")
    typer.echo(f"  data directory  {settings.data_dir}")
    typer.echo(f"  bind            {settings.host}:{settings.port}")
    typer.echo(f"  environment     {settings.env.value} (Live is not offered by `oft init`)")


def _warn_if_live(settings: Settings) -> None:
    """Never claim Paper when the process is pointed at the Live profile."""
    if settings.env == Environment.LIVE:
        typer.echo(
            "WARNING: `oft init` only configures Paper; no Live configuration "
            "is written (OFT_ENV=live).",
            err=True,
        )


def _print_providers(secrets: SecretsService) -> None:
    typer.echo(f"2/5 {_STEP_TITLES[2]}")
    for status in secrets.list_statuses():
        typer.echo(f"  {status.provider:<10} {_key_label(status)}")


def _print_deferred_steps() -> None:
    binance = _DEFERRED_STEPS[3]
    typer.echo(
        f"3/5 {_STEP_TITLES[3]}: deferred to Milestone {binance}; "
        "init never prompts for Binance tokens."
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
    settings: Settings,
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
    typer.echo(f"Environment: {settings.env.value}. Live is not offered by `oft init`.")


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


def _sanitized_token(token: str) -> str:
    """Render a user-supplied token without escapes or invisible characters."""
    cleaned = "".join(
        character for character in strip_control_characters(token) if character.isprintable()
    )
    return cleaned or "?"


def _render_tokens(tokens: list[str]) -> str:
    """Bound and sanitize the tokens echoed into an error message."""
    rendered = ", ".join(_sanitized_token(token) for token in tokens[:_MAX_REPORTED_TOKENS])
    hidden = len(tokens) - _MAX_REPORTED_TOKENS
    if hidden > 0:
        rendered = f"{rendered} (+{hidden} more)"
    return rendered


def _validate_symbols(registry: SymbolRegistry, symbols: tuple[str, ...]) -> None:
    invalid = [symbol for symbol in symbols if _WATCHLIST_SYMBOL_PATTERN.fullmatch(symbol) is None]
    if invalid:
        message = (
            f"invalid watchlist symbol(s): {_render_tokens(invalid)}; "
            "symbols must be 2-24 uppercase letters or digits"
        )
        raise SettingValidationError(
            message,
            details=[
                {"field": "watchlist", "code": "invalid_symbol", "message": message},
            ],
        )
    unknown = [symbol for symbol in symbols if registry.get_symbol(symbol) is None]
    if unknown:
        message = (
            f"unknown watchlist symbol(s): {_render_tokens(unknown)}; "
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
    completed = set(progress.completed_steps)
    incomplete = tuple(step for step in ACTIVE_ONBOARDING_STEPS if step not in completed)
    # ``all_complete`` ignores the optional step 4, so only an empty ACTIVE
    # incomplete set is terminal: a run after the required steps must still be
    # able to configure (or re-prompt for) the optional Telegram step.
    if not incomplete:
        _print_deferred_steps()
        print_telegram_line(container.telegram_service)
        _print_summary(container, service, secrets, budget_service, settings)
        return
    if options.resume:
        typer.echo(f"Resuming at step {progress.resumed_step}/5; completed steps are persisted.")
    for step in ACTIVE_ONBOARDING_STEPS:
        if step in completed:
            continue
        if step == 1:
            _print_environment(settings)
            service.complete_onboarding_step(1, actor="cli")
        elif step == 2:
            _step_llm_interactive(service, secrets)
        elif step == 4:
            step_telegram_interactive(
                container.telegram_service,
                service,
                prompt_text=_prompt_text,
                read_token=_read_telegram_token,
            )
        elif step == 5:
            _step_watchlist_interactive(container, service, budget_service)
    _print_deferred_steps()
    _print_summary(container, service, secrets, budget_service, settings)


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
            # A key injected through OFT_*_API_KEY already satisfies the step;
            # init then just points the active provider at it.
            configured = any(status.configured for status in secrets.list_statuses())
            if options.llm_provider is not None or not configured:
                missing.append("--llm-provider")
        elif not secrets.status(provider).configured and not options.llm_key_stdin:
            missing.append("--llm-key-stdin")

    telegram_plan, telegram_missing = plan_telegram(
        incomplete=incomplete,
        options=options.telegram,
        token_configured=secrets.status_for("telegram", "bot_token").configured,
    )
    missing.extend(telegram_missing)

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
        telegram=telegram_plan,
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
    completed = set(progress.completed_steps)
    incomplete = tuple(step for step in ACTIVE_ONBOARDING_STEPS if step not in completed)
    # ``all_complete`` ignores optional step 4: when only step 4 is left, fall
    # through so explicit Telegram flags are applied rather than silently
    # dropped, and an omitted flag set still skips (without completing) it.
    if not incomplete:
        _print_deferred_steps()
        print_telegram_line(container.telegram_service)
        _print_summary(container, service, secrets, budget_service, settings)
        return

    plan = _plan_non_interactive(container, service, secrets, budget_service, options)
    if options.resume:
        typer.echo(f"Resuming at step {progress.resumed_step}/5; completed steps are persisted.")
    _print_environment(settings)
    _print_providers(secrets)
    _print_deferred_steps()
    skip_telegram = 4 in plan.incomplete and not plan.telegram.configure
    print_telegram_line(container.telegram_service, skipped=skip_telegram)
    _print_watchlist_state(container, service, budget_service)

    # Validation finished above; now apply prerequisites, then progress.
    if 2 in plan.incomplete:
        provider = plan.provider
        # ``provider`` is None when an already-configured key is reused; the
        # active provider is selected below without reading stdin.
        if provider is not None and plan.key_stdin:
            key = _read_secret_key()
            secrets.set_key(provider, key, actor="cli")
            typer.echo(f"  {provider} key stored {_key_label(secrets.status(provider))}")
        _ensure_active_provider(service, secrets, provider)
    apply_telegram_plan(
        container.telegram_service,
        plan.telegram,
        read_token=_read_telegram_token,
    )
    if 5 in plan.incomplete:
        for symbol in plan.symbols:
            container.symbol_registry.set_star_with_audit(symbol, True, actor="cli")
        _apply_scan_period(service, plan.period)
        _apply_budget(budget_service, plan.budget, now=utc_timestamp(container.clock.now()))
    for step in plan.incomplete:
        if step == 4 and not plan.telegram.configure:
            # Omission of every Telegram flag skips the optional step; it must
            # never be marked complete without a token and a chat.
            continue
        service.complete_onboarding_step(step, actor="cli")
    _print_summary(container, service, secrets, budget_service, settings)


# -- entry point -------------------------------------------------------------


def _supplied_non_interactive_flags(options: _InitOptions) -> list[str]:
    """Flags the interactive wizard cannot honor; reject rather than ignore them."""
    candidates = (
        ("--llm-provider", options.llm_provider is not None),
        ("--llm-key-stdin", options.llm_key_stdin),
        ("--watchlist", options.watchlist is not None),
        ("--scan-period", options.scan_period is not None),
        ("--budget-daily", options.budget_daily is not None),
        ("--budget-weekly", options.budget_weekly is not None),
        ("--budget-monthly", options.budget_monthly is not None),
        ("--telegram-token-stdin", options.telegram.token_stdin),
        ("--telegram-chat-id", bool(options.telegram.chat_ids)),
        ("--telegram-user-id", bool(options.telegram.user_ids)),
    )
    return [flag for flag, supplied in candidates if supplied]


def _run(options: _InitOptions) -> None:
    if not options.non_interactive:
        supplied = _supplied_non_interactive_flags(options)
        if supplied:
            emit_error(
                "VALIDATION_FAILED",
                "these flags require --non-interactive: " + ", ".join(supplied),
                json_output=False,
            )
            raise typer.Exit(code=2)

    if options.non_interactive and options.llm_key_stdin and options.llm_provider is None:
        # A configured key can satisfy step 2 without naming a provider, but a
        # key supplied on stdin cannot be attributed to one: reject it rather
        # than silently dropping a user-supplied secret before any read/write.
        raise SettingValidationError(
            "--llm-key-stdin requires --llm-provider: the key cannot be attributed to a provider"
        )

    if not options.non_interactive and not _stdin_is_tty():
        emit_error(
            "VALIDATION_FAILED",
            "stdin is not a TTY; pass --non-interactive (or run `oft init` in an "
            "interactive terminal).",
            json_output=False,
        )
        raise typer.Exit(code=2)

    settings = load_settings()
    _warn_if_live(settings)
    try:
        container = build_container(settings, initialize=True)
    except OSError as exc:
        raise OSError(
            f"cannot create or use the data directory {settings.data_dir}; "
            "check the path and its permissions, then run `oft doctor`"
        ) from exc
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
            help=(
                "openai|anthropic|deepseek; --non-interactive only; required when "
                "step 2 is incomplete and no provider key is configured."
            ),
        ),
    ] = None,
    llm_key_stdin: Annotated[
        bool,
        typer.Option(
            "--llm-key-stdin",
            help=(
                "Read the provider key from stdin; --non-interactive only; a key "
                "is never accepted on argv."
            ),
        ),
    ] = False,
    watchlist: Annotated[
        str | None,
        typer.Option(
            "--watchlist",
            help=(
                "SYMBOL[,SYMBOL...] to star; --non-interactive only; required when "
                "no symbol is starred."
            ),
        ),
    ] = None,
    scan_period: Annotated[
        str | None,
        typer.Option("--scan-period", help="Scan period: 4h or 8h; --non-interactive only."),
    ] = None,
    budget_daily: Annotated[
        str | None,
        typer.Option("--budget-daily", help="Daily USD threshold; --non-interactive only."),
    ] = None,
    budget_weekly: Annotated[
        str | None,
        typer.Option("--budget-weekly", help="Weekly USD threshold; --non-interactive only."),
    ] = None,
    budget_monthly: Annotated[
        str | None,
        typer.Option("--budget-monthly", help="Monthly USD threshold; --non-interactive only."),
    ] = None,
    telegram_token_stdin: Annotated[
        bool,
        typer.Option(
            "--telegram-token-stdin",
            help=(
                "Read the optional Telegram bot token from stdin; never accepted on "
                "argv. Requires at least one --telegram-chat-id."
            ),
        ),
    ] = False,
    telegram_chat_id: Annotated[
        list[int] | None,
        typer.Option(
            "--telegram-chat-id",
            help="Telegram chat id to allow (repeatable; requires --telegram-token-stdin).",
        ),
    ] = None,
    telegram_user_id: Annotated[
        list[int] | None,
        typer.Option("--telegram-user-id", help="Telegram user id to allow (repeatable)."),
    ] = None,
) -> None:
    """Paper-only first-run onboarding; step 3 is deferred, step 4 is optional."""
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
        telegram=TelegramOptions(
            token_stdin=telegram_token_stdin,
            chat_ids=tuple(telegram_chat_id or ()),
            user_ids=tuple(telegram_user_id or ()),
        ),
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
    except StoreUnavailableError as exc:
        emit_error("DATA_UNAVAILABLE", exc.message, json_output=False)
        raise typer.Exit(code=1) from exc
    except SymbolRegistryError as exc:
        emit_error(exc.code, exc.message, json_output=False)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        emit_error(
            "CONFIG_INVALID",
            str(exc) or "cannot use the data directory; run `oft doctor`",
            json_output=False,
        )
        raise typer.Exit(code=5) from exc
    except Exception as exc:
        # Never leak a raw exception: its message may contain a secret.
        emit_error(
            "INTERNAL_ERROR",
            "unexpected error while running `oft init`; run `oft doctor`",
            json_output=False,
        )
        raise typer.Exit(code=1) from exc


__all__ = ["init_command"]
