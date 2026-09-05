"""Static import-graph guard: pipeline/ must never import generator/.

Per the component boundaries, this must be structurally impossible, not merely discouraged —
generator logic leaking into the graded path would invalidate every metric in the metric surface.

This is static analysis of the import graph (source parsed with ast, nothing
executed), not a runtime check. Every .py file under pipeline/ is scanned, so a
transitive import (pipeline.a -> pipeline.b -> generator) is caught because
pipeline.b's own file is scanned directly in the same pass.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = REPO_ROOT / "pipeline"
BANNED_ROOT = "generator"


def _imported_module_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import; cannot resolve to generator
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _pipeline_python_files() -> list[Path]:
    return sorted(PIPELINE_DIR.rglob("*.py"))


def test_pipeline_never_imports_generator():
    files = _pipeline_python_files()
    assert files, "expected at least one .py file under pipeline/"

    violations: dict[str, set[str]] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = _imported_module_roots(tree)
        if BANNED_ROOT in roots:
            violations[str(path.relative_to(REPO_ROOT))] = roots

    assert not violations, (
        f"pipeline/ modules must never import {BANNED_ROOT}/, directly or "
        f"transitively: {violations}"
    )
