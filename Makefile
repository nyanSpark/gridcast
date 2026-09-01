# Prefers uv when it is installed (https://docs.astral.sh/uv/), and falls back
# to a plain venv otherwise. Both produce the same environment.
PYTHON ?= python3
UV     := $(shell command -v uv 2>/dev/null)

ifdef UV
  GRIDCAST := uv run gridcast
  PYTEST   := uv run pytest
  RUFF     := uv run ruff
else
  GRIDCAST := .venv/bin/gridcast
  PYTEST   := .venv/bin/pytest
  RUFF     := .venv/bin/ruff
endif

.PHONY: help setup backfill update export snapshot serve static test lint clean

help:
	@echo "make setup     install dependencies (uv if present, else venv + pip)"
	@echo "make backfill  one year of CAISO + weather history (~10 min first run)"
	@echo "make update    incremental refresh (what CI runs)"
	@echo "make export    render static JSON into web/data/"
	@echo "make serve     API + frontend on http://127.0.0.1:8000"
	@echo "make static    preview the zero-backend build on http://127.0.0.1:8080"
	@echo "make test      run the test suite"
	@echo "make lint      ruff"

setup:
ifdef UV
	uv sync --extra dev
else
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -e ".[dev]"
endif
	$(GRIDCAST) init
	@echo "ready -- next: make backfill"

backfill:
	$(GRIDCAST) backfill --days 365 -l los-angeles -l fresno --verbose
	$(GRIDCAST) export
	$(GRIDCAST) snapshot

update:
	$(GRIDCAST) update --verbose
	$(GRIDCAST) export
	$(GRIDCAST) snapshot

export:
	$(GRIDCAST) export

snapshot:
	$(GRIDCAST) snapshot

serve:
	$(GRIDCAST) serve --reload

# Serves web/ with no API at all, so the frontend takes exactly the fallback
# path it will use on Vercel or GitHub Pages.
static: export
	$(PYTHON) -m http.server 8080 --directory web

test:
	$(PYTEST) -q

lint:
	$(RUFF) check src tests

clean:
	rm -rf .venv data/*.duckdb data/*.duckdb.wal data/cache .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
