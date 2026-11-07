"""Static guards on the CI workflow definition.

Every failure this file guards against actually reached a GitHub runner and cost a
red build. A workflow can be perfectly valid YAML and still be rejected by GitHub,
or be accepted and then fail in seconds on a step that has nothing to do with the
code under test. ``yaml.safe_load`` catches none of that, so these checks encode
the specific rules that were broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

#: Contexts GitHub allows in a workflow-level ``env:`` block. Notably absent:
#: ``runner`` (and ``steps``, ``job``, ``env``). Using one invalidates the whole file.
_ALLOWED_WORKFLOW_ENV_CONTEXTS = {"github", "inputs", "secrets", "vars"}


def _block(lines: list[str], key: str, indent: int = 0) -> list[str]:
    """Return the lines belonging to the ``key:`` block at the given indentation."""
    start = None
    prefix = " " * indent + key + ":"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            start = index
            break
    if start is None:
        return []
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            body.append(line)
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return body


def _parse_kv(lines: list[str], indent: int) -> dict[str, str]:
    """Parse a simple ``KEY: VALUE`` block at the given indentation."""
    result: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) != indent:
            continue
        key, _, value = line.strip().partition(":")
        result[key.strip()] = value.strip()
    return result


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW.is_file(), f"CI workflow is missing: {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow() -> dict:
    """A deliberately minimal view of the workflow, parsed without PyYAML.

    Only the handful of facts these tests need are extracted, so the guard adds no
    third-party dependency to the test surface it is meant to protect.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    return {
        "lines": lines,
        "env": _parse_kv(_block(lines, "env"), indent=2),
        "python_versions": re.findall(r'python-version:\s*"?([\d.]+)"?', text),
        "has_cache_pip": bool(re.search(r"^\s+cache:\s*pip", text, re.MULTILINE)),
        "run_commands": re.findall(r"^\s+run:\s*(.+)$", text, re.MULTILINE),
        "text": text,
    }


def _contexts(expression: str) -> set[str]:
    """Named-value roots used inside a ``${{ ... }}`` expression."""
    names: set[str] = set()
    for match in re.finditer(r"\$\{\{(.+?)\}\}", expression, re.DOTALL):
        body = match.group(1)
        for name in re.finditer(r"(?<![\w.'])([A-Za-z_][A-Za-z0-9_]*)\s*\.", body):
            names.add(name.group(1))
    return names


# ---------------------------------------------------------------------------
# The workflow-level env block: the trap that made the file invalid
# ---------------------------------------------------------------------------


def test_workflow_level_env_uses_only_permitted_contexts(workflow):
    """``runner`` is not available in a workflow-level `env:` block.

    Regression: ``GRAVO_WORKSPACE: ${{ runner.temp }}/...`` was set here, so GitHub
    rejected the entire workflow with "Unrecognized named-value: 'runner'" and no
    job was ever created — the failure looked like the tests but no test ran.
    """
    for key, value in workflow["env"].items():
        offending = _contexts(value) - _ALLOWED_WORKFLOW_ENV_CONTEXTS
        assert not offending, (
            f"workflow-level env var {key!r} references {sorted(offending)}; "
            f"only {sorted(_ALLOWED_WORKFLOW_ENV_CONTEXTS)} are allowed here. "
            "Move the value into a step-level env block instead."
        )


def test_runner_context_appears_only_at_step_level(workflow):
    """Any use of ``runner.*`` must live inside a step, not in workflow-wide config."""
    for line in workflow["lines"]:
        stripped = line.strip()
        if "runner." not in stripped or stripped.startswith("#"):
            continue
        # A step-level env line is indented far deeper than a job- or workflow-level one.
        indent = len(line) - len(line.lstrip())
        assert indent >= 8, f"'runner.' used at indentation {indent}, which is not step level: {stripped!r}"


# ---------------------------------------------------------------------------
# The environment that broke pip before any test ran
# ---------------------------------------------------------------------------


def test_no_global_pip_cache_switch(workflow):
    """``PIP_NO_CACHE_DIR`` must not be set globally.

    Regression: it was set at workflow level alongside setup-python's ``cache: pip``.
    That option shells out to ``pip cache dir``, which exits 1 when the cache is
    disabled — the job died in 6 seconds in the setup step, before ``make test``.
    """
    env = {k.upper() for k in workflow["env"]}
    assert "PIP_NO_CACHE_DIR" not in env, (
        "PIP_NO_CACHE_DIR at workflow level disables pip's cache globally, which "
        "breaks any step that queries it (e.g. setup-python cache: pip). The "
        "Makefile already passes --no-cache-dir per invocation."
    )


def test_pip_cache_option_is_not_combined_with_disabled_cache(workflow):
    """A step that asks pip about its cache cannot coexist with a disabled cache."""
    cache_disabled = "PIP_NO_CACHE_DIR" in {k.upper() for k in workflow["env"]}
    uses_cache = workflow["has_cache_pip"]
    assert not (cache_disabled and uses_cache), (
        "setup-python cache: pip runs `pip cache dir`, which fails when PIP_NO_CACHE_DIR is set. Remove one of the two."
    )


# ---------------------------------------------------------------------------
# Overall shape
# ---------------------------------------------------------------------------


def test_workflow_runs_the_required_steps_in_order(workflow):
    """Tests must run before lint, and both must be present."""
    runs = workflow["run_commands"]
    test_index = next(i for i, r in enumerate(runs) if "make test" in r)
    lint_index = next(i for i, r in enumerate(runs) if "make lint" in r)
    assert test_index < lint_index, "make test must run before make lint"


def test_workflow_pins_a_supported_python(workflow):
    """Python 3.13 is what the suite is proven on; requires-python allows >=3.10."""
    pinned = workflow["python_versions"]
    assert pinned, "no python-version pinned in any step"
    assert pinned[0].startswith("3.13"), f"expected 3.13, found {pinned[0]!r}"


def test_workflow_sets_a_workspace_override_for_the_monitored_tree(workflow):
    """The monitored tree does not exist on a runner; it must be redirected."""
    assert "GRAVO_WORKSPACE" in workflow["text"], (
        "CI must set GRAVO_WORKSPACE so egress footprint accounting has a real tree"
    )
