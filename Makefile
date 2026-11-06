PYTHON ?= python3
# Scratch space must never be the bounded /tmp tmpfs.
TMP_ROOT ?= /var/tmp

.PHONY: test lint build run-mcp clean hygiene-report hygiene-check

test:
	@set -eu; tmp="$$(mktemp -d "$(TMP_ROOT)/cad-fond-test.XXXXXX")"; trap 'rm -rf "$$tmp"' EXIT; \
	$(PYTHON) -m venv "$$tmp/venv"; \
	PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "$$tmp/venv/bin/python" -m pip install --no-cache-dir -e '.[test]'; \
	PYTHONDONTWRITEBYTECODE=1 "$$tmp/venv/bin/python" -m pytest -q -p no:cacheprovider tests; \
	PYTHONDONTWRITEBYTECODE=1 "$$tmp/venv/bin/python" -c 'from server.mcp_server import mcp; assert mcp is not None; print("MCP server import/schema registration: OK")'

lint:
	@set -eu; tmp="$$(mktemp -d "$(TMP_ROOT)/cad-fond-lint.XXXXXX")"; trap 'rm -rf "$$tmp"' EXIT; \
	$(PYTHON) -m venv "$$tmp/venv"; \
	PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "$$tmp/venv/bin/python" -m pip install --no-cache-dir ruff==0.17.3 mypy==2.4.1; \
	"$$tmp/venv/bin/ruff" check server tools/cad-fond-de-plan-build.py tools/footprint_providers.py tools/layout_engine.py tools/urban_topology.py tools/deliverable_qa.py tools/report_schema.py tools/acquisition.py tools/reconciliation.py tools/workspace_hygiene.py tools/check.py tools/egress_manager.py tests; \
	"$$tmp/venv/bin/ruff" format --check server tools/cad-fond-de-plan-build.py tools/footprint_providers.py tools/layout_engine.py tools/urban_topology.py tools/deliverable_qa.py tools/report_schema.py tools/acquisition.py tools/reconciliation.py tools/workspace_hygiene.py tools/check.py tools/egress_manager.py tests; \
	"$$tmp/venv/bin/mypy" server/mcp_server.py

build: clean
	@set -eu; tmp="$$(mktemp -d "$(TMP_ROOT)/cad-fond-build.XXXXXX")"; trap 'rm -rf "$$tmp"' EXIT; \
	$(PYTHON) -m venv "$$tmp/venv"; \
	PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "$$tmp/venv/bin/python" -m pip install --no-cache-dir build==1.3.0; \
	"$$tmp/venv/bin/python" -m build --outdir dist

run-mcp:
	uv run --project . python -m server.mcp_server

clean: ## remove regenerable caches and scratch virtualenvs (evidence is preserved)
	@$(PYTHON) tools/workspace_hygiene.py prune
	-@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

hygiene-report: ## show what is on disk, by tier
	@$(PYTHON) tools/workspace_hygiene.py report

hygiene-check: ## fail if regenerable clutter is present
	@$(PYTHON) tools/workspace_hygiene.py check
