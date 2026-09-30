from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
CLUSTER_PATH = ROOT / "scripts" / "cluster.py"
WORKLOAD_PATH = ROOT / "infra" / "kubernetes" / "workload.yaml"


def load_cluster() -> ModuleType:
    spec = importlib.util.spec_from_file_location("faultweld_cluster", CLUSTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cluster: Any = load_cluster()

MANAGED_NAMESPACE = json.dumps(
    {"metadata": {"name": "faultweld-demo", "labels": {"faultweld.dev/managed": "true"}}}
)
UNLABELLED_NAMESPACE = json.dumps({"metadata": {"name": "faultweld-demo", "labels": {}}})

DESTRUCTIVE_TOKENS = ("delete", "uninstall", "apply", "patch", "scale", "upgrade", "create")


def is_destructive(command: list[str]) -> bool:
    return any(token in command for token in DESTRUCTIVE_TOKENS)


class FakeKubectl:
    def __init__(self, context: str, namespace_json: str | None) -> None:
        self.calls: list[list[str]] = []
        self.context = context
        self.namespace_json = namespace_json

    def run(self, command: Sequence[str], *, capture: bool = False) -> str:
        cmd = list(command)
        self.calls.append(cmd)
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return self.context
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            if self.namespace_json is None:
                raise cluster.ClusterSafetyError("command failed: kubectl get namespace")
            return self.namespace_json
        return ""

    def assert_no_destructive_commands(self) -> None:
        destructive = [cmd for cmd in self.calls if is_destructive(cmd)]
        assert destructive == [], f"destructive commands ran: {destructive}"


def project_fake() -> FakeKubectl:
    return FakeKubectl("kind-faultweld", MANAGED_NAMESPACE)


class ClusterIsolationTests(TestCase):
    def test_project_context_and_namespace_are_accepted(self) -> None:
        fake = project_fake()
        with patch.object(cluster, "run", side_effect=fake.run):
            cluster.require_project_context()
            cluster.require_project_namespace()
        self.assertEqual(
            fake.calls,
            [
                ["kubectl", "config", "current-context"],
                ["kubectl", "config", "current-context"],
                ["kubectl", "get", "namespace", "faultweld-demo", "-o", "json"],
            ],
        )

    def test_teardown_targets_only_the_faultweld_cluster(self) -> None:
        fake = project_fake()
        with patch.object(cluster, "run", side_effect=fake.run):
            with patch.object(cluster, "require_tools", return_value=None):
                cluster.teardown()
        self.assertEqual(
            fake.calls,
            [
                ["kubectl", "config", "current-context"],
                ["kubectl", "get", "namespace", "faultweld-demo", "-o", "json"],
                [
                    "helm",
                    "uninstall",
                    "faultweld-demo",
                    "--namespace",
                    "faultweld-demo",
                    "--ignore-not-found",
                    "--wait",
                    "--timeout=30m",
                ],
                [
                    "kubectl",
                    "delete",
                    "namespace",
                    "faultweld-demo",
                    "--wait=true",
                    "--timeout=120s",
                ],
                ["kind", "delete", "cluster", "--name", "faultweld"],
            ],
        )

    def test_teardown_wrong_context_rejected_before_mutation(self) -> None:
        fake = FakeKubectl("kind-other", MANAGED_NAMESPACE)
        with patch.object(cluster, "run", side_effect=fake.run):
            with patch.object(cluster, "require_tools", return_value=None):
                with self.assertRaisesRegex(cluster.ClusterSafetyError, "refusing"):
                    cluster.teardown()
        self.assertEqual(fake.calls, [["kubectl", "config", "current-context"]])
        fake.assert_no_destructive_commands()

    def test_teardown_unlabelled_namespace_rejected_before_mutation(self) -> None:
        fake = FakeKubectl("kind-faultweld", UNLABELLED_NAMESPACE)
        with patch.object(cluster, "run", side_effect=fake.run):
            with patch.object(cluster, "require_tools", return_value=None):
                with self.assertRaisesRegex(cluster.ClusterSafetyError, "unlabelled"):
                    cluster.teardown()
        self.assertEqual(
            [cmd[:3] for cmd in fake.calls],
            [["kubectl", "config", "current-context"], ["kubectl", "get", "namespace"]],
        )
        fake.assert_no_destructive_commands()

    def test_teardown_missing_namespace_rejected_before_mutation(self) -> None:
        fake = FakeKubectl("kind-faultweld", None)
        with patch.object(cluster, "run", side_effect=fake.run):
            with patch.object(cluster, "require_tools", return_value=None):
                with self.assertRaises(cluster.ClusterSafetyError):
                    cluster.teardown()
        fake.assert_no_destructive_commands()

    def test_reset_rejected_outside_project_context(self) -> None:
        fake = FakeKubectl("kind-production", MANAGED_NAMESPACE)
        with patch.object(cluster, "run", side_effect=fake.run):
            with self.assertRaisesRegex(cluster.ClusterSafetyError, "refusing"):
                cluster.reset()
        self.assertEqual(fake.calls, [["kubectl", "config", "current-context"]])
        fake.assert_no_destructive_commands()

    def test_reset_commands_stay_scoped_to_project(self) -> None:
        fake = project_fake()
        with patch.object(cluster, "run", side_effect=fake.run):
            cluster.reset()
        kubectl_calls = [cmd for cmd in fake.calls if cmd and cmd[0] == "kubectl"]
        workload_calls = [
            cmd
            for cmd in kubectl_calls
            if cmd[1:3] != ["config", "current-context"] and cmd[1:3] != ["get", "namespace"]
        ]
        self.assertTrue(workload_calls)
        for cmd in workload_calls:
            self.assertIn("-n", cmd)
            self.assertIn("faultweld-demo", cmd)
        self.assertTrue(any("deployment/faultweld-sample" in cmd for cmd in workload_calls))
        self.assertFalse(any("kube-system" in cmd for cmd in fake.calls))

    def test_setup_refuses_existing_cluster_on_wrong_context(self) -> None:
        fake = FakeKubectl("kind-other", MANAGED_NAMESPACE)

        class KindResult:
            returncode = 0
            stdout = "faultweld\n"
            stderr = ""

        with patch.object(cluster, "run", side_effect=fake.run):
            with patch.object(cluster.subprocess, "run", return_value=KindResult()):
                with self.assertRaisesRegex(cluster.ClusterSafetyError, "refusing"):
                    cluster.setup()
        fake.assert_no_destructive_commands()


def manifest_documents() -> list[str]:
    text = WORKLOAD_PATH.read_text(encoding="utf-8")
    return [doc for doc in re.split(r"(?m)^---\s*$", text) if doc.strip()]


def named_resource_document(kind: str, name: str) -> str:
    for doc in manifest_documents():
        if not re.search(rf"(?m)^kind:\s*{re.escape(kind)}\s*$", doc):
            continue
        metadata_name = re.search(r"(?m)^  name:\s*([^\s]+)\s*$", doc)
        if metadata_name and metadata_name.group(1) == name:
            return doc
    raise AssertionError(f"{kind} {name} not found in workload.yaml")


def role_rules(doc: str) -> set[tuple[frozenset[str], frozenset[str], frozenset[str]]]:
    pattern = re.compile(
        r"(?m)^[ \t]*-[ \t]*apiGroups:[ \t]*\[(.*?)\][ \t]*\r?\n"
        r"[ \t]+resources:[ \t]*\[(.*?)\][ \t]*\r?\n"
        r"[ \t]+verbs:[ \t]*\[(.*?)\][ \t]*$"
    )

    def values(value: str) -> frozenset[str]:
        return frozenset(part.strip().strip("\"'") for part in value.split(",") if part.strip())

    return {
        (values(api_groups), values(resources), values(verbs))
        for api_groups, resources, verbs in pattern.findall(doc)
    }


class WorkloadRbacTests(TestCase):
    def test_no_cluster_scoped_roles_or_bindings(self) -> None:
        text = WORKLOAD_PATH.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?m)^kind:\s*ClusterRole(Binding)?\s*$", text))

    def test_agent_identity_is_read_only(self) -> None:
        self.assertEqual(
            role_rules(named_resource_document("Role", "faultweld-agent-readonly")),
            {
                (
                    frozenset({""}),
                    frozenset({"pods", "services"}),
                    frozenset({"get", "list", "watch"}),
                ),
                (
                    frozenset({"apps"}),
                    frozenset({"deployments"}),
                    frozenset({"get", "list", "watch"}),
                ),
            },
        )

    def test_controller_permissions_are_narrowly_scoped(self) -> None:
        self.assertEqual(
            role_rules(named_resource_document("Role", "faultweld-scenario-controller")),
            {
                (
                    frozenset({""}),
                    frozenset({"pods"}),
                    frozenset({"get", "list", "watch", "delete"}),
                ),
                (frozenset({""}), frozenset({"services"}), frozenset({"get", "list", "watch"})),
                (
                    frozenset({"apps"}),
                    frozenset({"deployments", "deployments/scale"}),
                    frozenset({"get", "list", "watch", "patch", "update"}),
                ),
            },
        )

    def test_rolebindings_match_declared_service_accounts(self) -> None:
        for name in ("faultweld-scenario-controller", "faultweld-agent-readonly"):
            service_account = named_resource_document("ServiceAccount", name)
            self.assertRegex(service_account, r"(?m)^  namespace: faultweld-demo$")

            binding = named_resource_document("RoleBinding", name)
            subject = re.search(
                r"(?ms)^subjects:\s*\n\s*- kind:\s*ServiceAccount\s*\n"
                r"\s+name:\s*([^\s]+)\s*\n\s+namespace:\s*([^\s]+)",
                binding,
            )
            role_ref = re.search(
                r"(?ms)^roleRef:\s*\n\s+kind:\s*Role\s*\n"
                r"\s+name:\s*([^\s]+)\s*\n\s+apiGroup:",
                binding,
            )
            self.assertIsNotNone(subject, f"{name} binding has no ServiceAccount subject")
            assert subject is not None
            self.assertEqual(subject.groups(), (name, "faultweld-demo"))
            self.assertIsNotNone(role_ref, f"{name} binding does not reference a Role")
            assert role_ref is not None
            self.assertEqual(role_ref.group(1), name)

    def test_manifest_confines_every_namespaced_object(self) -> None:
        namespaces = re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", WORKLOAD_PATH.read_text())
        self.assertTrue(namespaces)
        self.assertEqual(set(namespaces), {"faultweld-demo"})
