#!/usr/bin/env python3
"""Deterministic package QA for a Fond de Plan export. JSON stdout; 0 pass, 1 fail.

All checks live in :mod:`deliverable_qa`; this module is the CLI shell. The MCP
``verify_topology`` tool and the builder call the same functions, so every
consumer returns one verdict for one artifact set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deliverable_qa import QA_CONTRACT_VERSION, qa_output_directory  # noqa: E402


def run(path: Path) -> dict:
    return qa_output_directory(path, include_report=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Fond de Plan deliverables")
    parser.add_argument("path", help="output directory or file")
    parser.add_argument("--json", action="store_true", help="emit JSON (default also JSON)")
    args = parser.parse_args()
    result = run(Path(args.path).expanduser())
    result.setdefault("qa_contract_version", QA_CONTRACT_VERSION)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
