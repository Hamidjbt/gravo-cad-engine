PYTHON ?= python3
TMP_ROOT ?= /tmp

.PHONY: test lint build run-mcp clean

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
	"$$tmp/venv/bin/ruff" check server tools/layout_engine.py tools/urban_topology.py; \
	"$$tmp/venv/bin/ruff" format --check server tools/layout_engine.py tools/urban_topology.py; \
	"$$tmp/venv/bin/mypy" server/mcp_server.py

build:
	@set -eu; tmp="$$(mktemp -d "$(TMP_ROOT)/cad-fond-build.XXXXXX")"; trap 'rm -rf "$$tmp"' EXIT; \
	$(PYTHON) -m venv "$$tmp/venv"; \
	PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "$$tmp/venv/bin/python" -m pip install --no-cache-dir build==1.3.0; \
	"$$tmp/venv/bin/python" -m build --outdir dist

run-mcp:
	uv run --project . python -m server.mcp_server

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
