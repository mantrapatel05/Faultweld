from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

from app.sim.cluster import SimCluster, SimSource

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "backend" / "app"
ALLOWED_DIRS = {"sim", "gateway"}
ALLOWED_FILES = {ROOT / "scripts" / "lite.py"}
WRITE_AUTHORITY = ("ClusterWriter", "SimWriter", "apply_restart", "apply_scale", "apply_rollback")


class GatewayAuthorityTests(TestCase):
    def test_write_authority_names_are_confined_to_sim_gateway_and_composition_root(self) -> None:
        violations: list[str] = []
        for path in sorted(APP.rglob("*.py")):
            if path.parent.name in ALLOWED_DIRS:
                continue
            source = path.read_text(encoding="utf-8")
            for name in WRITE_AUTHORITY:
                if name in source:
                    violations.append(f"{path.relative_to(ROOT)} mentions {name}")
        for path in ALLOWED_FILES:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            self.assertIn("SimWriter", imported)
        self.assertEqual(violations, [])

    def test_read_only_sim_source_still_exposes_only_snapshot(self) -> None:
        source = SimCluster().source
        self.assertIsInstance(source, SimSource)
        self.assertEqual(
            {name for name in dir(source) if not name.startswith("_")}, {"snapshot"}
        )

    def test_gateway_does_not_accept_action_parameters_from_its_caller(self) -> None:
        from inspect import signature

        from app.gateway import Gateway

        self.assertEqual(
            set(signature(Gateway.execute).parameters),
            {"self", "proposal_id", "caller_principal_id"},
        )
