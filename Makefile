.PHONY: install test lint typecheck check web-install web-dev web-test web-lint web-typecheck web-build serve

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

check: lint typecheck test web-lint web-typecheck web-test web-build
