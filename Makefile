UV ?= uv
RUN = $(UV) run --no-sync

.PHONY: install check lint typecheck test build dist-check
install:
	$(UV) sync --frozen --all-extras

check: lint typecheck test

lint:
	$(RUN) ruff check src tests scripts examples
	$(RUN) ruff format --check src tests scripts examples

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest -q

build:
	$(UV) build

dist-check: build
	$(RUN) twine check dist/*
	$(RUN) python scripts/check_distribution.py
