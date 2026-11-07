.PHONY: install test lint typecheck check web-install web-dev web-test web-lint web-typecheck web-build serve import-linter fixtures coverage ci e2e

install:
	uv sync
	npm --prefix web install

serve:
	uv run oft serve

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

import-linter:
	uv run lint-imports

fixtures:
	uv run python scripts/fetch-fixtures verify

coverage:
	uv run pytest -q --cov=oft --cov-branch --cov-report=term-missing --cov-fail-under=80
	uv run python scripts/check-metric-coverage

web-install:
	npm --prefix web install

web-dev:
	npm --prefix web run dev

web-test:
	npm --prefix web run test

web-lint:
	npm --prefix web run lint

web-typecheck:
	npm --prefix web run typecheck

web-build:
	npm --prefix web run build

# Offline CI equivalent: no network, no daemons, no live exchange/LLM access.
ci: lint typecheck import-linter fixtures coverage web-lint web-typecheck web-test web-build

# Playwright E2E starts three loopback daemons in temporary data dirs.
e2e: web-build
	npm --prefix web run test:e2e

check: lint typecheck test web-lint web-typecheck web-test web-build
