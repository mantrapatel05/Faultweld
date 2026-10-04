from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
KIND_CONFIG = ROOT / "infra" / "kind" / "kind-config.yaml"
KUBERNETES_DIR = ROOT / "infra" / "kubernetes"
CLUSTER_NAME = "faultweld"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "faultweld-demo"
SAMPLE_DEPLOYMENT = "faultweld-sample"
SAMPLE_IMAGE = "hashicorp/http-echo:1.0.0"
OTEL_RELEASE = "faultweld-demo"
OTEL_CHART = "opentelemetry-demo"
OTEL_CHART_VERSION = "0.42.0"
OTEL_CHART_REPOSITORY = "https://open-telemetry.github.io/opentelemetry-helm-charts"
HELM_TIMEOUT = "30m"
DEPLOYMENT_PROGRESS_DEADLINE_SECONDS = 1800


class ClusterSafetyError(RuntimeError):
    pass


def require_tools(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise ClusterSafetyError(f"required command-line tools are missing: {', '.join(missing)}")


def run(command: Sequence[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=capture,
            text=True,
        )
    except OSError as error:
        raise ClusterSafetyError(f"could not run {command[0]!r}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else ""
        suffix = f": {detail}" if detail else ""
        raise ClusterSafetyError(f"command failed: {' '.join(command)}{suffix}")
    return result.stdout.strip() if capture else ""


def current_context() -> str:
    return run(["kubectl", "config", "current-context"], capture=True)


def require_project_context() -> None:
    context = current_context()
    if context != CONTEXT:
        raise ClusterSafetyError(
            f"refusing operation outside context {CONTEXT!r}; found {context!r}"
        )


def require_project_namespace() -> None:
    require_project_context()
    labels = run(
        ["kubectl", "get", "namespace", NAMESPACE, "-o", "json"],
        capture=True,
    )
    metadata: dict[str, Any] = json.loads(labels).get("metadata", {})
    if metadata.get("labels", {}).get("faultweld.dev/managed") != "true":
        raise ClusterSafetyError(f"refusing operation on unlabelled namespace {NAMESPACE!r}")


def setup() -> None:
    require_tools("docker", "kind", "kubectl", "helm")
    existing = subprocess.run(
        ["kind", "get", "clusters"], check=False, capture_output=True, text=True
    )
    if existing.returncode != 0:
        raise ClusterSafetyError(f"could not list kind clusters: {existing.stderr.strip()}")
    if CLUSTER_NAME not in existing.stdout.splitlines():
        run(["kind", "create", "cluster", "--config", str(KIND_CONFIG)])
    else:
        require_project_namespace()
    require_project_context()
    make_control_planes_schedulable()
    load_sample_image()
    run(["kubectl", "apply", "-f", str(KUBERNETES_DIR)])
    install_demo()
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{SAMPLE_DEPLOYMENT}",
            "--timeout=120s",
        ]
    )


def reset() -> None:
    require_project_namespace()
    run(["kubectl", "-n", NAMESPACE, "scale", f"deployment/{SAMPLE_DEPLOYMENT}", "--replicas=1"])
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "patch",
            f"deployment/{SAMPLE_DEPLOYMENT}",
            "--type=json",
            "-p",
            '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/"}]',
        ]
    )
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "patch",
            f"deployment/{SAMPLE_DEPLOYMENT}",
            "--type=json",
            "-p",
            '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/port","value":"http"}]',
        ]
    )
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{SAMPLE_DEPLOYMENT}",
            "--timeout=120s",
        ]
    )


def make_control_planes_schedulable() -> None:
    nodes: dict[str, Any] = json.loads(
        run(
            [
                "kubectl",
                "get",
                "nodes",
                "-l",
                "node-role.kubernetes.io/control-plane",
                "-o",
                "json",
            ],
            capture=True,
        )
    )
    for node in nodes.get("items", []):
        name = node.get("metadata", {}).get("name")
        taints = node.get("spec", {}).get("taints", [])
        if name and any(
            taint.get("key") == "node-role.kubernetes.io/control-plane"
            and taint.get("effect") == "NoSchedule"
            for taint in taints
        ):
            run(
                [
                    "kubectl",
                    "taint",
                    "node",
                    name,
                    "node-role.kubernetes.io/control-plane-",
                ]
            )


def load_sample_image() -> None:
    run(["docker", "pull", "--platform=linux/amd64", SAMPLE_IMAGE])
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as archive:
        archive_path = Path(archive.name)
    try:
        run(
            [
                "docker",
                "save",
                "--platform",
                "linux/amd64",
                "--output",
                str(archive_path),
                SAMPLE_IMAGE,
            ]
        )
        run(["kind", "load", "image-archive", str(archive_path), "--name", CLUSTER_NAME])
    finally:
        archive_path.unlink(missing_ok=True)


def teardown() -> None:
    require_tools("kind", "kubectl", "helm")
    require_project_namespace()
    run(
        [
            "helm",
            "uninstall",
            OTEL_RELEASE,
            "--namespace",
            NAMESPACE,
            "--ignore-not-found",
            "--wait",
            f"--timeout={HELM_TIMEOUT}",
        ]
    )
    run(["kubectl", "delete", "namespace", NAMESPACE, "--wait=true", "--timeout=120s"])
    run(["kind", "delete", "cluster", "--name", CLUSTER_NAME])


def install_demo() -> None:
    run(
        [
            "helm",
            "upgrade",
            "--install",
            OTEL_RELEASE,
            OTEL_CHART,
            "--repo",
            OTEL_CHART_REPOSITORY,
            "--version",
            OTEL_CHART_VERSION,
            "--namespace",
            NAMESPACE,
            f"--timeout={HELM_TIMEOUT}",
            "--values",
            str(ROOT / "infra" / "helm" / "opentelemetry-demo-values.yaml"),
        ]
    )
    deployments = json.loads(
        run(
            ["kubectl", "-n", NAMESPACE, "get", "deployments", "-o", "json"],
            capture=True,
        )
    )
    for deployment in deployments.get("items", []):
        name = deployment.get("metadata", {}).get("name")
        if name:
            run(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "patch",
                    "deployment",
                    name,
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {
                            "spec": {
                                "progressDeadlineSeconds": DEPLOYMENT_PROGRESS_DEADLINE_SECONDS
                            }
                        }
                    ),
                ]
            )
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod",
            "--all",
            f"--timeout={HELM_TIMEOUT}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the disposable Faultweld kind cluster.")
    parser.add_argument("command", choices=("setup", "reset", "teardown"))
    args = parser.parse_args()
    try:
        {"setup": setup, "reset": reset, "teardown": teardown}[args.command]()
    except ClusterSafetyError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
