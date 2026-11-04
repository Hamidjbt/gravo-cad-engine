#!/usr/bin/env python3
"""Portable, underscore-named CLI alias for the read-only A3 zoom exporter."""
from __future__ import annotations
import importlib.util
from pathlib import Path

TARGET = Path(__file__).with_name("cad-fond-de-plan-zoom-a3.py")
spec = importlib.util.spec_from_file_location("cad_fond_de_plan_zoom_a3", TARGET)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load A3 exporter implementation: {TARGET}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if __name__ == "__main__":
    raise SystemExit(module.main())
