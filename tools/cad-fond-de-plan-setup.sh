#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
VENV="${CAD_FOND_DE_PLAN_VENV:-$HOME/.venv/cad-fond-de-plan}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("cad-fond-de-plan requires Python 3.10 or newer")
PY
mkdir -p "$(dirname "$VENV")"
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
"$VENV/bin/python" - <<'PY'
import cairosvg, ezdxf, geopandas, osmnx, pandas, PIL, pyproj, requests, scipy, shapely, svgwrite
print("CAD Fond de Plan geospatial/CAD stack ready")
PY
printf 'Environment: %s\n' "$VENV"
printf 'Run build: %s/bin/python %s/tools/cad-fond-de-plan-build.py --help\n' "$VENV" "$ROOT"
printf 'Run A3: %s/bin/python %s/tools/a3_layout_exporter.py --help\n' "$VENV" "$ROOT"
