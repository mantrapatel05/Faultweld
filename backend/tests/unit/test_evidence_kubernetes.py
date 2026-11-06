from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from unittest import TestCase

from app.domain.schemas import EvidenceFreshness, EvidenceSourceType
from app.evidence.kubernetes import (
    KubernetesEvidenceAdapter,
    KubernetesQuery,
    ResourceKind,
    canonical_json,
    request_path,
)
from app.evidence.models import EvidenceContext, EvidenceItem, EvidenceReadError, ReadOutcome
from app.evidence.transport import KubernetesTransport, TransportError

NS = "faultweld-demo"
FIXED = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CONTEXT = EvidenceContext(incident_id="incident-1", run_id="run-1")
SAMPLE_LABEL = {"app.kubernetes.io/name": "faultweld-sample"}


class FakeTransport:
    def __init__(self, responses: dict[str, str | TransportError]) -> None:
        self.responses = responses
        self.paths: list[str] = []

    def get(self, path: str) -> str:
        self.paths.append(path)
        outcome = self.responses[path]
        if isinstance(outcome, TransportError):
            raise outcome
        return outcome


def make_adapter(
    responses: dict[str, str | TransportError], **limits: int
) -> tuple[KubernetesEvidenceAdapter, FakeTransport]:
    transport = FakeTransport(responses)
    adapter = KubernetesEvidenceAdapter(transport, now=lambda: FIXED, **limits)
    return adapter, transport


def as_json(item: EvidenceItem) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(canonical_json(item.payload))
    return loaded


def deployment_object() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "faultweld-sample",
            "namespace": NS,
            "uid": "uid-dep-1",
            "resourceVersion": "101",
            "generation": 3,
            "creationTimestamp": "2026-10-03T08:00:00Z",
            "labels": {**SAMPLE_LABEL, "app.kubernetes.io/part-of": "faultweld"},
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "password=hunter2"},
            "managedFields": [{"manager": "kubectl"}],
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": SAMPLE_LABEL},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "http",
                            "image": "hashicorp/http-echo:1.0.0",
                            "args": ["-text=ok"],
                            "env": [{"name": "API_KEY", "value": "canary-secret-value"}],
                            "readinessProbe": {"httpGet": {"path": "/", "port": "http"}},
                            "livenessProbe": {"httpGet": {"path": "/", "port": "http"}},
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 3,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "conditions": [
                {
                    "type": "Available",
                    "status": "True",
                    "reason": "MinimumReplicasAvailable",
                    "message": "Deployment has minimum availability.",
                    "lastTransitionTime": "2026-10-03T08:00:05Z",
                    "lastUpdateTime": "2026-10-03T08:00:05Z",
                }
            ],
        },
    }


def pod_object(name: str = "faultweld-sample-abc12-xyz98", namespace: str = NS) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": "uid-pod-1",
            "resourceVersion": "202",
            "creationTimestamp": "2026-10-03T08:00:00Z",
            "labels": SAMPLE_LABEL,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "faultweld-sample-abc12",
                    "uid": "uid-rs-1",
                    "controller": True,
                }
            ],
            "annotations": {"note": "token=abc123"},
            "managedFields": [{"manager": "kubelet"}],
        },
        "spec": {
            "nodeName": "faultweld-worker",
            "containers": [
                {
                    "name": "http",
                    "image": "hashicorp/http-echo:1.0.0",
                    "env": [{"name": "X", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}],
                }
            ],
            "volumes": [{"name": "kube-api-access", "projected": {}}],
        },
        "status": {
            "phase": "Running",
            "startTime": "2026-10-03T08:00:01Z",
            "podIP": "10.244.1.5",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "http",
                    "image": "hashicorp/http-echo:1.0.0",
                    "imageID": "docker.io/hashicorp/http-echo@sha256:abc",
                    "ready": True,
                    "restartCount": 2,
                    "state": {"running": {"startedAt": "2026-10-03T08:00:02Z"}},
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
        },
    }


def service_object() -> dict[str, Any]:
    return {
        "kind": "Service",
        "metadata": {"name": "faultweld-sample", "namespace": NS, "uid": "uid-svc-1"},
        "spec": {
            "type": "ClusterIP",
            "clusterIP": "10.96.0.10",
            "selector": SAMPLE_LABEL,
            "ports": [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}],
        },
    }


def list_response(kind: str, items: list[dict[str, Any]], cont: str | None = None) -> str:
    metadata: dict[str, str] = {"resourceVersion": "9"}
    if cont is not None:
        metadata["continue"] = cont
    return json.dumps({"kind": f"{kind}List", "metadata": metadata, "items": items})


DEPLOYMENT_QUERY = KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name="faultweld-sample")
POD_QUERY = KubernetesQuery(kind=ResourceKind.POD, label_selector="app.kubernetes.io/name=faultweld-sample")
SERVICE_QUERY = KubernetesQuery(kind=ResourceKind.SERVICE, name="faultweld-sample")


class DeploymentEvidenceTests(TestCase):
    def read_deployment(self, document: dict[str, Any]) -> EvidenceItem:
        adapter, _ = make_adapter({request_path(DEPLOYMENT_QUERY): json.dumps(document)})
        result = adapter.read(CONTEXT, DEPLOYMENT_QUERY)
        self.assertIs(result.outcome, ReadOutcome.OK)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.items), 1)
        return result.items[0]

    def test_payload_is_the_exact_whitelisted_projection(self) -> None:
        item = self.read_deployment(deployment_object())
        self.assertEqual(
            as_json(item),
            {
                "metadata": {
                    "name": "faultweld-sample",
                    "namespace": NS,
                    "uid": "uid-dep-1",
                    "resourceVersion": "101",
                    "generation": 3,
                    "creationTimestamp": "2026-10-03T08:00:00Z",
                    "labels": {**SAMPLE_LABEL, "app.kubernetes.io/part-of": "faultweld"},
                    "ownerReferences": [],
                },
                "spec": {
                    "replicas": 1,
                    "strategy": "Recreate",
                    "selector": SAMPLE_LABEL,
                    "containers": [
                        {
                            "name": "http",
                            "image": "hashicorp/http-echo:1.0.0",
                            "readinessProbePath": "/",
                            "livenessProbePath": "/",
                        }
                    ],
                },
                "status": {
                    "observedGeneration": 3,
                    "replicas": 1,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                    "unavailableReplicas": 0,
                    "conditions": [
                        {
                            "type": "Available",
                            "status": "True",
                            "reason": "MinimumReplicasAvailable",
                            "message": "Deployment has minimum availability.",
                            "lastTransitionTime": "2026-10-03T08:00:05Z",
                        }
                    ],
                },
            },
        )

    def test_omitted_zero_counters_are_reported_as_zero(self) -> None:
        document = deployment_object()
        document["spec"]["replicas"] = 0
        document["status"] = {"observedGeneration": 4}
        status = as_json(self.read_deployment(document))["status"]
        self.assertEqual(
            status,
            {
                "observedGeneration": 4,
                "replicas": 0,
                "updatedReplicas": 0,
                "readyReplicas": 0,
                "availableReplicas": 0,
                "unavailableReplicas": 0,
                "conditions": [],
            },
        )

    def test_the_readiness_probe_path_is_visible_as_evidence(self) -> None:
        document = deployment_object()
        containers = document["spec"]["template"]["spec"]["containers"]
        containers[0]["readinessProbe"]["httpGet"]["path"] = "/unhealthy"
        payload = as_json(self.read_deployment(document))
        self.assertEqual(payload["spec"]["containers"][0]["readinessProbePath"], "/unhealthy")

    def test_record_fields_follow_the_domain_contract(self) -> None:
        item = self.read_deployment(deployment_object())
        record = item.record
        digest = hashlib.sha256(canonical_json(item.payload).encode("utf-8")).hexdigest()
        self.assertEqual(record.schema_version, "0.1")
        self.assertIs(record.source_type, EvidenceSourceType.KUBERNETES)
        self.assertIs(record.freshness, EvidenceFreshness.FRESH)
        self.assertEqual(record.source_resource, "Deployment/faultweld-demo/faultweld-sample")
        self.assertEqual(
            record.query,
            "GET apis/apps/v1/deployments namespace=faultweld-demo name=faultweld-sample",
        )
        self.assertEqual(record.observed_at, FIXED)
        self.assertEqual((record.incident_id, record.run_id), ("incident-1", "run-1"))
        self.assertEqual(record.content_hash, digest)
        self.assertTrue(record.evidence_id.startswith("ev-k8s-"))
        self.assertEqual(record.payload_reference, f"evidence-store/{record.evidence_id}")
        self.assertIsNone(record.retrieval_score)
        self.assertIsNone(record.runbook_revision)

    def test_evidence_ids_are_stable_for_equal_state_and_differ_by_run_and_state(self) -> None:
        first = self.read_deployment(deployment_object()).record
        second = self.read_deployment(deployment_object()).record
        self.assertEqual(first.evidence_id, second.evidence_id)

        adapter, _ = make_adapter(
            {request_path(DEPLOYMENT_QUERY): json.dumps(deployment_object())}
        )
        other_run = EvidenceContext(incident_id="incident-1", run_id="run-2")
        self.assertNotEqual(
            adapter.read(other_run, DEPLOYMENT_QUERY).items[0].record.evidence_id,
            first.evidence_id,
        )

        changed = deployment_object()
        changed["spec"]["replicas"] = 0
        changed_record = self.read_deployment(changed).record
        self.assertNotEqual(changed_record.content_hash, first.content_hash)
        self.assertNotEqual(changed_record.evidence_id, first.evidence_id)

    def test_sensitive_and_noisy_fields_never_reach_the_payload(self) -> None:
        item = self.read_deployment(deployment_object())
        serialized = canonical_json(item.payload) + repr(item.record)
        for needle in (
            "hunter2",
            "canary-secret-value",
            "API_KEY",
            "managedFields",
            "annotations",
            "last-applied",
            "-text=ok",
            "env",
        ):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, serialized)

    def test_condition_messages_are_redacted_and_bounded(self) -> None:
        document = deployment_object()
        document["status"]["conditions"] = [
            {"type": "Progressing", "message": "pull failed: token=abc123 for registry"},
            {"type": "Other", "message": "x" * 500},
        ]
        conditions = as_json(self.read_deployment(document))["status"]["conditions"]
        self.assertNotIn("abc123", conditions[0]["message"])
        self.assertIn("token=[redacted]", conditions[0]["message"])
        self.assertEqual(len(conditions[1]["message"]), 200)
        self.assertTrue(conditions[1]["message"].endswith("…"))


class PodAndServiceEvidenceTests(TestCase):
    def test_pod_payload_projects_state_and_drops_noise(self) -> None:
        adapter, _ = make_adapter(
            {request_path(POD_QUERY): list_response("Pod", [pod_object()])}
        )
        result = adapter.read(CONTEXT, POD_QUERY)
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        payload = as_json(item)
        self.assertEqual(item.record.source_resource, f"Pod/{NS}/faultweld-sample-abc12-xyz98")
        self.assertEqual(
            payload["status"]["containerStatuses"],
            [
                {
                    "name": "http",
                    "image": "hashicorp/http-echo:1.0.0",
                    "ready": True,
                    "restartCount": 2,
                    "state": {"phase": "running"},
                    "lastState": {"phase": "terminated", "reason": "Error", "exitCode": 1},
                }
            ],
        )
        self.assertEqual(payload["status"]["phase"], "Running")
        self.assertEqual(
            payload["metadata"]["ownerReferences"],
            [{"kind": "ReplicaSet", "name": "faultweld-sample-abc12", "controller": True}],
        )
        serialized = canonical_json(item.payload)
        for needle in ("abc123", "podIP", "10.244.1.5", "imageID", "secretKeyRef", "volumes"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, serialized)

    def test_waiting_container_reports_its_reason(self) -> None:
        pod = pod_object()
        pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ContainerCreating"}}
        pod["status"]["containerStatuses"][0]["ready"] = False
        adapter, _ = make_adapter({request_path(POD_QUERY): list_response("Pod", [pod])})
        status = as_json(adapter.read(CONTEXT, POD_QUERY).items[0])["status"]["containerStatuses"][0]
        self.assertEqual(status["state"], {"phase": "waiting", "reason": "ContainerCreating"})
        self.assertFalse(status["ready"])

    def test_service_payload_omits_cluster_address(self) -> None:
        adapter, _ = make_adapter({request_path(SERVICE_QUERY): json.dumps(service_object())})
        item = adapter.read(CONTEXT, SERVICE_QUERY).items[0]
        self.assertEqual(
            as_json(item)["spec"],
            {
                "type": "ClusterIP",
                "selector": SAMPLE_LABEL,
                "ports": [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}],
            },
        )
        self.assertNotIn("10.96.0.10", canonical_json(item.payload))


class ListAndBoundsTests(TestCase):
    def three_pods(self) -> list[dict[str, Any]]:
        return [pod_object(name=f"faultweld-sample-{suffix}") for suffix in ("c", "a", "b")]

    def test_items_are_sorted_by_name(self) -> None:
        adapter, _ = make_adapter(
            {request_path(POD_QUERY): list_response("Pod", self.three_pods())}
        )
        names = [item.record.source_resource for item in adapter.read(CONTEXT, POD_QUERY).items]
        self.assertEqual(names, [f"Pod/{NS}/faultweld-sample-{suffix}" for suffix in "abc"])

    def test_an_empty_list_is_ok_and_not_truncated(self) -> None:
        adapter, _ = make_adapter({request_path(POD_QUERY): list_response("Pod", [])})
        result = adapter.read(CONTEXT, POD_QUERY)
        self.assertIs(result.outcome, ReadOutcome.OK)
        self.assertEqual((result.items, result.truncated), ((), False))

    def test_a_continue_token_marks_the_read_truncated(self) -> None:
        adapter, _ = make_adapter(
            {request_path(POD_QUERY): list_response("Pod", [pod_object()], cont="abc")}
        )
        result = adapter.read(CONTEXT, POD_QUERY)
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.truncated)

    def test_a_server_returning_more_than_the_limit_is_cut_and_flagged(self) -> None:
        query = KubernetesQuery(kind=ResourceKind.POD, limit=1)
        adapter, _ = make_adapter(
            {request_path(query): list_response("Pod", self.three_pods())}
        )
        result = adapter.read(CONTEXT, query)
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.truncated)

    def test_total_evidence_bytes_are_budgeted(self) -> None:
        responses: dict[str, str | TransportError] = {
            request_path(POD_QUERY): list_response("Pod", self.three_pods())
        }
        adapter, _ = make_adapter(responses)
        full = adapter.read(CONTEXT, POD_QUERY)
        size = len(canonical_json(full.items[0].payload).encode("utf-8"))
        self.assertFalse(full.truncated)

        limited, _ = make_adapter(responses, max_total_bytes=size * 2)
        result = limited.read(CONTEXT, POD_QUERY)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(result.truncated)

    def test_an_oversized_payload_fails_closed(self) -> None:
        adapter, _ = make_adapter(
            {request_path(DEPLOYMENT_QUERY): json.dumps(deployment_object())},
            max_payload_bytes=10,
        )
        with self.assertRaises(EvidenceReadError) as raised:
            adapter.read(CONTEXT, DEPLOYMENT_QUERY)
        self.assertEqual(raised.exception.code, "payload_too_large")

    def test_an_oversized_response_fails_closed(self) -> None:
        adapter, _ = make_adapter(
            {request_path(DEPLOYMENT_QUERY): json.dumps(deployment_object())},
            max_response_bytes=10,
        )
        with self.assertRaises(EvidenceReadError) as raised:
            adapter.read(CONTEXT, DEPLOYMENT_QUERY)
        self.assertEqual(raised.exception.code, "response_too_large")


class IsolationAndFailureTests(TestCase):
    def code_for(self, response: str | TransportError, query: KubernetesQuery) -> str:
        adapter, _ = make_adapter({request_path(query): response})
        with self.assertRaises(EvidenceReadError) as raised:
            adapter.read(CONTEXT, query)
        return raised.exception.code

    def test_an_object_from_another_namespace_is_rejected(self) -> None:
        foreign = pod_object(namespace="kube-system")
        code = self.code_for(list_response("Pod", [pod_object(), foreign]), POD_QUERY)
        self.assertEqual(code, "cross_namespace_object")

    def test_a_named_read_returning_another_namespace_is_rejected(self) -> None:
        document = deployment_object()
        document["metadata"]["namespace"] = "kube-system"
        self.assertEqual(
            self.code_for(json.dumps(document), DEPLOYMENT_QUERY), "cross_namespace_object"
        )

    def test_an_object_without_a_namespace_is_rejected(self) -> None:
        document = deployment_object()
        del document["metadata"]["namespace"]
        self.assertEqual(
            self.code_for(json.dumps(document), DEPLOYMENT_QUERY), "cross_namespace_object"
        )

    def test_a_named_read_returning_a_different_object_is_malformed(self) -> None:
        document = deployment_object()
        document["metadata"]["name"] = "something-else"
        self.assertEqual(self.code_for(json.dumps(document), DEPLOYMENT_QUERY), "malformed_response")

    def test_malformed_responses_are_rejected(self) -> None:
        wrong_kind = json.dumps({**deployment_object(), "kind": "Pod"})
        cases = {
            "not json": "{",
            "not an object": "[]",
            "wrong kind": wrong_kind,
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.code_for(body, DEPLOYMENT_QUERY), "malformed_response")
        list_cases = {
            "items not a list": json.dumps({"kind": "PodList", "items": {}}),
            "item not an object": json.dumps({"kind": "PodList", "items": ["x"]}),
            "wrong list kind": json.dumps({"kind": "DeploymentList", "items": []}),
        }
        for label, body in list_cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.code_for(body, POD_QUERY), "malformed_response")

    def test_a_missing_name_is_malformed(self) -> None:
        document = deployment_object()
        del document["metadata"]["name"]
        self.assertEqual(self.code_for(json.dumps(document), DEPLOYMENT_QUERY), "malformed_response")

    def test_not_found_is_an_outcome_not_evidence(self) -> None:
        for query in (DEPLOYMENT_QUERY, POD_QUERY):
            with self.subTest(query=query.kind):
                adapter, _ = make_adapter(
                    {request_path(query): TransportError("not_found", "missing")}
                )
                result = adapter.read(CONTEXT, query)
                self.assertIs(result.outcome, ReadOutcome.NOT_FOUND)
                self.assertEqual(result.items, ())

    def test_forbidden_and_unreachable_sources_raise_stable_codes(self) -> None:
        self.assertEqual(
            self.code_for(TransportError("forbidden", "denied"), DEPLOYMENT_QUERY), "forbidden"
        )
        self.assertEqual(
            self.code_for(TransportError("unavailable", "down"), DEPLOYMENT_QUERY),
            "source_unavailable",
        )

    def test_a_naive_clock_is_rejected(self) -> None:
        transport = FakeTransport({request_path(DEPLOYMENT_QUERY): json.dumps(deployment_object())})
        adapter = KubernetesEvidenceAdapter(transport, now=lambda: datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            adapter.read(CONTEXT, DEPLOYMENT_QUERY)
        self.assertEqual(transport.paths, [])


class ReadOnlyContractTests(TestCase):
    def test_transport_protocol_exposes_only_get(self) -> None:
        public = {name for name in dir(KubernetesTransport) if not name.startswith("_")}
        self.assertEqual(public, {"get"})

    def test_every_request_is_a_namespaced_read_path(self) -> None:
        adapter, transport = make_adapter(
            {
                request_path(DEPLOYMENT_QUERY): json.dumps(deployment_object()),
                request_path(POD_QUERY): list_response("Pod", [pod_object()]),
                request_path(SERVICE_QUERY): json.dumps(service_object()),
            }
        )
        for query in (DEPLOYMENT_QUERY, POD_QUERY, SERVICE_QUERY):
            adapter.read(CONTEXT, query)
        self.assertEqual(len(transport.paths), 3)
        for path in transport.paths:
            with self.subTest(path=path):
                self.assertRegex(
                    path,
                    r"^/(api/v1|apis/apps/v1)/namespaces/faultweld-demo/"
                    r"(deployments|pods|services)",
                )
                self.assertNotIn("secrets", path)
