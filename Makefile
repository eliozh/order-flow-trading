.PHONY: install test lint typecheck check web-install web-dev web-test web-lint web-typecheck web-build serve import-linter fixtures coverage ci e2e wheel-check

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
ci: lint typecheck import-linter fixtures coverage web-lint web-typecheck web-test web-build wheel-check

# Build the wheel, assert its Requires-Dist metadata covers every runtime
# import, then install it into a throwaway venv outside the repo and run the
# CLI. The lock export pins exact versions; the install may fetch those pins
# from the package index when the CI cache is cold.
wheel-check:
	@set -eu; \
	D=$$(mktemp -d "$${TMPDIR:-/tmp}/oft-wheel-check.XXXXXX"); \
	trap 'rm -rf "$$D"' EXIT; \
	uv build --wheel --out-dir "$$D" >/dev/null; \
	W=$$(ls "$$D"/oft-*.whl); \
	uv run python scripts/check_wheel_deps.py --wheel "$$W"; \
	uv export --frozen --no-dev --no-emit-project --format requirements-txt > "$$D/requirements.txt"; \
	uv venv --python 3.12 "$$D/venv" >/dev/null; \
	uv pip install --python "$$D/venv/bin/python" -r "$$D/requirements.txt" "$$W" >/dev/null; \
	"$$D/venv/bin/oft" --help >/dev/null; \
	OFT_DATA=$$(mktemp -d "$$D/data.XXXXXX") "$$D/venv/bin/oft" status --json >/dev/null; \
	echo "wheel-check: built wheel installs and runs in a clean venv"

# Playwright E2E starts three loopback daemons in temporary data dirs.
e2e: web-build
	npm --prefix web run test:e2e

check: lint typecheck test web-lint web-typecheck web-test web-build
