from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[3]
TELEMETRY_PATH = ROOT / "infra" / "kubernetes" / "telemetry.yaml"
NAMESPACE = "faultweld-demo"


def text() -> str:
    return TELEMETRY_PATH.read_text(encoding="utf-8")


def documents() -> list[str]:
    return [doc for doc in re.split(r"(?m)^---\s*$", text()) if doc.strip()]


def named(kind: str, name: str) -> str:
    for doc in documents():
        if not re.search(rf"(?m)^kind:\s*{re.escape(kind)}\s*$", doc):
            continue
        metadata_name = re.search(r"(?m)^  name:\s*([^\s]+)\s*$", doc)
        if metadata_name and metadata_name.group(1) == name:
            return doc
    raise AssertionError(f"{kind} {name} not found in telemetry.yaml")


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


def dashboard() -> dict[str, Any]:
    doc = named("ConfigMap", "faultweld-grafana-dashboards")
    match = re.search(r"(?ms)^  faultweld-sample\.json: \|\n(.*)\Z", doc)
    assert match is not None
    loaded: dict[str, Any] = json.loads(textwrap.dedent(match.group(1)))
    return loaded


class TelemetryIsolationTests(TestCase):
    def test_no_cluster_scoped_objects(self) -> None:
        self.assertIsNone(re.search(r"(?m)^kind:\s*ClusterRole(Binding)?\s*$", text()))
        self.assertIsNone(re.search(r"(?m)^kind:\s*Namespace\s*$", text()))

    def test_every_namespaced_object_is_confined_to_the_project_namespace(self) -> None:
        namespaces = re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", text())
        self.assertTrue(namespaces)
        self.assertEqual(set(namespaces), {NAMESPACE})

    def test_kube_state_metrics_is_read_only_and_narrow(self) -> None:
        self.assertEqual(
            role_rules(named("Role", "faultweld-kube-state-metrics")),
            {
                (frozenset({""}), frozenset({"pods"}), frozenset({"list", "watch"})),
                (frozenset({"apps"}), frozenset({"deployments"}), frozenset({"list", "watch"})),
            },
        )
        deployment = named("Deployment", "faultweld-kube-state-metrics")
        self.assertIn("--namespaces=faultweld-demo", deployment)
        self.assertIn("--resources=deployments,pods", deployment)
        self.assertNotRegex(deployment, r"--namespaces=\s*$")

    def test_no_write_verbs_anywhere(self) -> None:
        verbs = " ".join(re.findall(r"(?m)^\s+verbs:\s*\[(.*?)\]", text()))
        for forbidden in ("create", "update", "patch", "delete", "deletecollection", "*"):
            self.assertNotIn(f'"{forbidden}"', verbs)

    def test_only_kube_state_metrics_receives_an_api_token(self) -> None:
        self.assertNotRegex(
            named("ServiceAccount", "faultweld-kube-state-metrics"),
            r"(?m)^automountServiceAccountToken:",
        )
        for name in ("faultweld-otel-collector", "faultweld-prometheus", "faultweld-grafana"):
            with self.subTest(name=name):
                self.assertRegex(
                    named("ServiceAccount", name),
                    r"(?m)^automountServiceAccountToken:\s*false\s*$",
                )

    def test_images_are_pinned_to_explicit_versions(self) -> None:
        images = re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", text())
        self.assertEqual(len(images), 4)
        for image in images:
            with self.subTest(image=image):
                self.assertNotIn("latest", image)
                self.assertRegex(image, r"^[a-z0-9./_-]+:v?\d+\.\d+\.\d+$")

    def test_no_privileged_or_host_access(self) -> None:
        self.assertNotRegex(
            text(), r"(?i)hostPath|hostNetwork|hostPID|privileged:\s*true|docker\.sock"
        )
        self.assertNotRegex(text(), r"(?i)\bsecrets?\b")


class TelemetryWiringTests(TestCase):
    def test_prometheus_scrapes_the_collector_export_port(self) -> None:
        config = named("ConfigMap", "faultweld-prometheus-config")
        self.assertIn("faultweld-otel-collector:8889", config)
        self.assertIn("honor_labels: true", config)
        service = named("Service", "faultweld-otel-collector")
        self.assertRegex(service, r"(?m)^\s+port:\s*8889\s*$")

    def test_collector_scrapes_kube_state_metrics_and_expires_stale_series(self) -> None:
        config = named("ConfigMap", "faultweld-otel-collector-config")
        self.assertIn("faultweld-kube-state-metrics:8080", config)
        self.assertIn("job_name: kube-state-metrics", config)
        self.assertIn("metric_expiration: 20s", config)
        service = named("Service", "faultweld-kube-state-metrics")
        self.assertRegex(service, r"(?m)^\s+port:\s*8080\s*$")

    def test_source_outage_rule_exists(self) -> None:
        rules = named("ConfigMap", "faultweld-prometheus-config")
        self.assertIn("alert: FaultweldKubeStateMetricsUnavailable", rules)
        self.assertIn("absent(up{job=\"kube-state-metrics\"})", rules)

    def test_grafana_datasource_matches_the_prometheus_service(self) -> None:
        datasource = named("ConfigMap", "faultweld-grafana-datasources")
        self.assertIn("url: http://faultweld-prometheus:9090", datasource)
        self.assertRegex(named("Service", "faultweld-prometheus"), r"(?m)^\s+port:\s*9090\s*$")


class DashboardTests(TestCase):
    def test_dashboard_is_valid_json_with_a_stable_uid(self) -> None:
        loaded = dashboard()
        self.assertEqual(loaded["uid"], "faultweld-sample")
        self.assertGreaterEqual(len(loaded["panels"]), 6)
        self.assertFalse(loaded["editable"])

    def test_every_panel_uses_the_provisioned_datasource(self) -> None:
        datasource = named("ConfigMap", "faultweld-grafana-datasources")
        match = re.search(r"(?m)^\s+uid:\s*(\S+)\s*$", datasource)
        assert match is not None
        for panel in dashboard()["panels"]:
            with self.subTest(panel=panel["title"]):
                self.assertEqual(panel["datasource"]["uid"], match.group(1))

    def test_queries_stay_inside_the_project_namespace(self) -> None:
        expressions = [
            target["expr"] for panel in dashboard()["panels"] for target in panel["targets"]
        ]
        self.assertTrue(expressions)
        namespaces = {
            found for expr in expressions for found in re.findall(r"namespace='([^']+)'", expr)
        }
        self.assertEqual(namespaces, {NAMESPACE})

    def test_dashboard_shows_replica_state_and_source_health(self) -> None:
        joined = " ".join(
            target["expr"] for panel in dashboard()["panels"] for target in panel["targets"]
        )
        for needle in (
            "kube_deployment_status_replicas_available",
            "kube_deployment_spec_replicas",
            "kube_pod_status_ready",
            "up{job='kube-state-metrics'}",
            "up{job='otel-collector'}",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, joined)

    def test_missing_data_is_labelled_not_shown_as_a_number(self) -> None:
        for panel in dashboard()["panels"]:
            if panel["type"] == "stat":
                with self.subTest(panel=panel["title"]):
                    self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "no data")
