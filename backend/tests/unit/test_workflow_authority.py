from __future__ import annotations

import ast
from pathlib import Path


def test_workflow_does_not_import_writer_or_mutable_simulator() -> None:
    root = Path(__file__).parents[2] / "app" / "workflow"
    forbidden = {"SimWriter", "ClusterWriter", "SimCluster", "subprocess", "httpx"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for node in node.names
        }
        names.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not forbidden.intersection(names), path
