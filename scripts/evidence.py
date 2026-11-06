"""Faultweld Kubernetes evidence checks (read-only operator tooling).

``read`` prints evidence records and payloads for one allow-listed query.
``verify`` proves, against the live disposable cluster and as the read-only agent
identity, that evidence is produced, that other namespaces and Secrets are not readable,
and that payloads omit sensitive fields. Nothing here mutates the cluster.
Run with PYTHONPATH pointing at ``backend``, as for scripts/scenario.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from app.evidence.kubernetes import (
    ALLOWED_NAMESPACE,
    KubernetesEvidenceAdapter,
    KubernetesQuery,
    ResourceKind,
    canonical_json,
)
from app.evidence.models import EvidenceContext, EvidenceItem, EvidenceReadError, ReadOutcome
from app.evidence.transport import (
    AGENT_IDENTITY,
    KIND_CONTEXT,
    KubectlRawTransport,
    TransportError,
)

CONTEXT = KIND_CONTEXT
NAMESPACE = ALLOWED_NAMESPACE
SAMPLE_DEPLOYMENT = "faultweld-sample"
SELECTOR = f"app.kubernetes.io/name={SAMPLE_DEPLOYMENT}"
CHECK_CONTEXT = EvidenceContext(incident_id="evidence-check", run_id="evidence-check-run")
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"data", "stringData", "binaryData", "env", "envFrom", "annotations", "managedFields", "args"}
)


class EvidenceCheckError(RuntimeError):
    pass


def run(command: Sequence[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(list(command), check=False, capture_output=capture, text=True)
    except OSError as error:
        raise EvidenceCheckError(f"could not run {command[0]!r}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else ""
        suffix = f": {detail}" if detail else ""
        raise EvidenceCheckError(f"command failed: {' '.join(command)}{suffix}")
    return result.stdout.strip() if capture else ""


def require_scope() -> None:
    context = run(["kubectl", "config", "current-context"], capture=True)
    if context != CONTEXT:
        raise EvidenceCheckError(
            f"refusing operation outside context {CONTEXT!r}; found {context!r}"
        )
    namespace: dict[str, Any] = json.loads(
        run(["kubectl", "get", "namespace", NAMESPACE, "-o", "json"], capture=True)
    )
    labels = namespace.get("metadata", {}).get("labels", {})
    if labels.get("faultweld.dev/managed") != "true":
        raise EvidenceCheckError(f"refusing operation on unlabelled namespace {NAMESPACE!r}")


def can_i(verb: str, resource: str, namespace: str) -> bool:
    command = [
        "kubectl",
        "--context",
        CONTEXT,
        "auth",
        "can-i",
        verb,
        resource,
        "-n",
        namespace,
        "--as",
        AGENT_IDENTITY,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as error:
        raise EvidenceCheckError(f"could not run 'kubectl': {error}") from error
    if result.returncode not in (0, 1):
        raise EvidenceCheckError(f"auth can-i failed: {result.stderr.strip()}")
    return result.stdout.strip() == "yes"


def forbidden_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_PAYLOAD_KEYS:
                found.add(str(key))
            found |= forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            found |= forbidden_keys(child)
    return found


def payload_value(payload: object, *path: str) -> object:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def content_hash_matches(item: EvidenceItem) -> bool:
    digest = hashlib.sha256(canonical_json(item.payload).encode("utf-8")).hexdigest()
    return digest == item.record.content_hash


def check_deployment(adapter: KubernetesEvidenceAdapter) -> tuple[list[str], list[EvidenceItem]]:
    result = adapter.read(
        CHECK_CONTEXT, KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name=SAMPLE_DEPLOYMENT)
    )
    if result.outcome is not ReadOutcome.OK or len(result.items) != 1:
        return ["expected exactly one Deployment evidence item"], list(result.items)
    item = result.items[0]
    failures: list[str] = []
    expected = f"Deployment/{NAMESPACE}/{SAMPLE_DEPLOYMENT}"
    if item.record.source_resource != expected:
        failures.append(f"unexpected source_resource {item.record.source_resource!r}")
    if item.record.freshness.value != "fresh":
        failures.append("a read taken now must be fresh")
    if not content_hash_matches(item):
        failures.append("content_hash does not match the payload")
    if payload_value(item.payload, "metadata", "namespace") != NAMESPACE:
        failures.append("payload namespace is not the project namespace")
    if not payload_value(item.payload, "spec", "containers"):
        failures.append("payload lists no containers")
    return failures, list(result.items)


def check_pods(adapter: KubernetesEvidenceAdapter) -> tuple[list[str], list[EvidenceItem]]:
    result = adapter.read(
        CHECK_CONTEXT, KubernetesQuery(kind=ResourceKind.POD, label_selector=SELECTOR)
    )
    failures: list[str] = []
    if result.outcome is not ReadOutcome.OK or not result.items:
        failures.append("expected at least one sample Pod (is the baseline healthy?)")
    for item in result.items:
        if not item.record.source_resource.startswith(f"Pod/{NAMESPACE}/{SAMPLE_DEPLOYMENT}-"):
            failures.append(f"unexpected pod {item.record.source_resource!r}")
        if payload_value(item.payload, "metadata", "namespace") != NAMESPACE:
            failures.append("pod payload is outside the project namespace")
        if not content_hash_matches(item):
            failures.append("pod content_hash does not match the payload")
    return failures, list(result.items)


def check_service(adapter: KubernetesEvidenceAdapter) -> tuple[list[str], list[EvidenceItem]]:
    result = adapter.read(
        CHECK_CONTEXT, KubernetesQuery(kind=ResourceKind.SERVICE, name=SAMPLE_DEPLOYMENT)
    )
    if result.outcome is not ReadOutcome.OK or len(result.items) != 1:
        return ["expected exactly one Service evidence item"], list(result.items)
    item = result.items[0]
    failures: list[str] = []
    if not content_hash_matches(item):
        failures.append("service content_hash does not match the payload")
    if not payload_value(item.payload, "spec", "ports"):
        failures.append("service payload lists no ports")
    return failures, list(result.items)


def check_missing(adapter: KubernetesEvidenceAdapter) -> list[str]:
    result = adapter.read(
        CHECK_CONTEXT,
        KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name="faultweld-does-not-exist"),
    )
    if result.outcome is not ReadOutcome.NOT_FOUND or result.items:
        return ["a missing resource must report not_found with no evidence"]
    return []


def check_rbac(transport: KubectlRawTransport) -> list[str]:
    failures: list[str] = []
    forbidden_reads = (
        (f"/api/v1/namespaces/{NAMESPACE}/secrets", "secrets in the project namespace"),
        ("/api/v1/namespaces/kube-system/pods", "pods in kube-system"),
    )
    for path, label in forbidden_reads:
        try:
            transport.get(path)
        except TransportError as error:
            if error.code != "forbidden":
                failures.append(f"{label}: expected forbidden, got {error.code}")
        else:
            failures.append(f"{label}: the read succeeded")
    for verb, resource in (
        ("get", "secrets"),
        ("list", "secrets"),
        ("patch", "deployments"),
        ("update", "deployments"),
        ("delete", "pods"),
        ("create", "pods"),
    ):
        if can_i(verb, resource, NAMESPACE):
            failures.append(f"agent identity can {verb} {resource}")
    if can_i("get", "pods", "kube-system"):
        failures.append("agent identity can get pods in kube-system")
    if not can_i("get", "deployments", NAMESPACE):
        failures.append("control: agent identity cannot get deployments in the project namespace")
    return failures


def check_structure(items: Sequence[EvidenceItem]) -> list[str]:
    if not items:
        return ["no payloads were collected to inspect"]
    found: set[str] = set()
    for item in items:
        found |= forbidden_keys(json.loads(canonical_json(item.payload)))
    return [f"payload contains forbidden field {key!r}" for key in sorted(found)]


def run_check(name: str, check: Callable[[], list[str]]) -> dict[str, Any]:
    try:
        failures = check()
    except (EvidenceReadError, TransportError, EvidenceCheckError) as error:
        failures = [f"{type(error).__name__}: {error}"]
    return {"name": name, "passed": not failures, "failures": failures}


def verify() -> dict[str, Any]:
    require_scope()
    transport = KubectlRawTransport()
    adapter = KubernetesEvidenceAdapter(transport)
    collected: list[EvidenceItem] = []

    def deployment() -> list[str]:
        failures, items = check_deployment(adapter)
        collected.extend(items)
        return failures

    def pods() -> list[str]:
        failures, items = check_pods(adapter)
        collected.extend(items)
        return failures

    def service() -> list[str]:
        failures, items = check_service(adapter)
        collected.extend(items)
        return failures

    checks = [
        run_check("deployment evidence", deployment),
        run_check("pod evidence", pods),
        run_check("service evidence", service),
        run_check("missing resource", lambda: check_missing(adapter)),
        run_check(
            "agent identity is read-only and namespace-bound", lambda: check_rbac(transport)
        ),
        run_check("payloads omit sensitive fields", lambda: check_structure(collected)),
    ]
    return {
        "environment_id": CONTEXT,
        "namespace": NAMESPACE,
        "identity": AGENT_IDENTITY,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }


def read(kind: str, name: str | None, selector: str | None, limit: int) -> dict[str, Any]:
    query = KubernetesQuery(
        kind=ResourceKind(kind), name=name, label_selector=selector, limit=limit
    )
    result = KubernetesEvidenceAdapter(KubectlRawTransport()).read(
        EvidenceContext(incident_id="evidence-cli", run_id="evidence-cli-run"), query
    )
    return {
        "outcome": result.outcome.value,
        "truncated": result.truncated,
        "items": [
            {"record": item.record.model_dump(mode="json"), "payload": item.payload}
            for item in result.items
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read or verify Faultweld Kubernetes evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    read_parser = commands.add_parser("read", help="print evidence for one allow-listed query")
    read_parser.add_argument("kind", choices=[kind.value for kind in ResourceKind])
    read_parser.add_argument("--name")
    read_parser.add_argument("--selector")
    read_parser.add_argument("--limit", type=int, default=20)
    commands.add_parser("verify", help="live read-only checks against the disposable cluster")
    args = parser.parse_args()

    try:
        require_scope()
        if args.command == "verify":
            report = verify()
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["passed"] else 1
        print(
            json.dumps(
                read(args.kind, args.name, args.selector, args.limit), sort_keys=True, indent=2
            )
        )
        return 0
    except (EvidenceCheckError, EvidenceReadError, TransportError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
