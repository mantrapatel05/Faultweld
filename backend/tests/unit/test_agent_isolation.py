from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = ROOT / "backend" / "app" / "agent"

ALLOWED_APP_MODULES = frozenset(
    {
        "app.domain.schemas",
        "app.evidence.models",
        "app.evidence.canonical",
        "app.sim.cluster",
    }
)
ALLOWED_SIM_CLUSTER_NAMES = frozenset({"EvidenceSource", "SimSnapshot"})
FORBIDDEN_MODULES = frozenset(
    {"os", "socket", "http", "urllib", "requests", "httpx", "subprocess"}
)
FORBIDDEN_NAMES = frozenset({"SimCluster", "SimWriter", "ClusterWriter"})
FORBIDDEN_TEXT = frozenset({"scenarios/expected", "expected_outcome", "allowed_actions"})


def violations(source: str, filename: str = "<memory>") -> list[str]:
    """Return human-readable import/text violations found in one module source."""
    tree = ast.parse(source, filename=filename)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                problems.extend(_module_problems(alias.name, ()))
                if alias.name in FORBIDDEN_NAMES:
                    problems.append(f"forbidden import name {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = tuple(alias.name for alias in node.names)
            if node.level:
                if module and not module.startswith("app.agent"):
                    problems.append(f"relative import escapes app.agent: {module}")
                continue
            problems.extend(_module_problems(module, names))
            for name in names:
                if name in FORBIDDEN_NAMES:
                    problems.append(f"forbidden import name {name}")
    for marker in FORBIDDEN_TEXT:
        if marker in source:
            problems.append(f"forbidden text {marker}")
    return problems


def _module_problems(module: str, names: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    root = module.split(".", 1)[0]
    if root in FORBIDDEN_MODULES:
        problems.append(f"forbidden module {module}")
    if module == "app" or module.startswith("app."):
        if not module.startswith("app.agent") and module not in ALLOWED_APP_MODULES:
            problems.append(f"app module outside the read-only allow-list: {module}")
    if module == "app.sim.cluster":
        unknown = sorted(set(names) - ALLOWED_SIM_CLUSTER_NAMES)
        if unknown:
            problems.append(f"forbidden names imported from app.sim.cluster: {unknown}")
    return problems


class AgentIsolationTests(TestCase):
    def test_every_agent_module_is_within_the_read_only_import_allow_list(self) -> None:
        paths = sorted(AGENT_DIR.glob("*.py"))
        self.assertIn(AGENT_DIR / "investigator.py", paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertEqual(
                    violations(path.read_text(encoding="utf-8"), str(path)),
                    [],
                )

    def test_the_scanner_catches_a_write_path_import(self) -> None:
        problems = violations("import subprocess\n")
        self.assertTrue(any("subprocess" in problem for problem in problems))

    def test_the_scanner_catches_a_cluster_writer_import(self) -> None:
        problems = violations("from app.sim.cluster import SimCluster\n")
        self.assertTrue(any("SimCluster" in problem for problem in problems))

    def test_the_scanner_catches_the_kubernetes_adapter(self) -> None:
        problems = violations("from app.evidence.kubernetes import canonical_json\n")
        self.assertTrue(any("app.evidence.kubernetes" in problem for problem in problems))

    def test_the_scanner_catches_store_and_gateway_imports(self) -> None:
        for module in ("app.store", "app.gateway"):
            with self.subTest(module=module):
                problems = violations(f"from {module} import anything\n")
                self.assertTrue(problems)

    def test_the_scanner_catches_evaluator_label_text(self) -> None:
        problems = violations('LABELS = "scenarios/expected/labels.json"\n')
        self.assertTrue(any("scenarios/expected" in problem for problem in problems))

    def test_the_scanner_catches_a_relative_import_escape(self) -> None:
        problems = violations("from ..store import Store\n")
        self.assertTrue(problems)
