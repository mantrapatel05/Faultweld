from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.evidence.redaction import REDACTED, redact_text
from app.evidence.transport import (
    AGENT_IDENTITY,
    KIND_CONTEXT,
    KubectlRawTransport,
    TransportError,
    classify_stderr,
    summarize_stderr,
)

ROOT = Path(__file__).resolve().parents[3]
WORKLOAD_PATH = ROOT / "infra" / "kubernetes" / "workload.yaml"
PATH = "/api/v1/namespaces/faultweld-demo/pods?limit=1"


def completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class RedactionTests(TestCase):
    def test_credentials_are_redacted(self) -> None:
        cases = {
            "token=abc123": "token=[redacted]",
            "password: hunter2": "password=[redacted]",
            "api_key=zzz": "api_key=[redacted]",
            "x-API-KEY: zzz": "x-API-KEY=[redacted]",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(redact_text(original), expected)

    def test_bearer_tokens_and_jwts_are_removed(self) -> None:
        bearer = redact_text("Authorization: Bearer abc.def-123")
        self.assertNotIn("abc.def-123", bearer)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"
        self.assertEqual(redact_text(f"saw {jwt} here"), f"saw {REDACTED} here")

    def test_ordinary_text_is_unchanged(self) -> None:
        for text in (
            "Deployment has minimum availability.",
            "no secrets here",
            'secret "x" not found',
            "readiness probe failed",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)


class KubectlTransportTests(TestCase):
    def test_default_command_is_pinned_to_context_and_agent_identity(self) -> None:
        with patch("app.evidence.transport.subprocess.run", return_value=completed(0, "{}")) as run:
            self.assertEqual(KubectlRawTransport().get(PATH), "{}")
        self.assertEqual(
            run.call_args.args[0],
            ["kubectl", "--context", KIND_CONTEXT, "--as", AGENT_IDENTITY, "get", "--raw", PATH],
        )

    def test_impersonation_can_be_disabled_explicitly(self) -> None:
        command = KubectlRawTransport(impersonate=None).command(PATH)
        self.assertEqual(command, ["kubectl", "--context", KIND_CONTEXT, "get", "--raw", PATH])

    def test_only_get_raw_is_ever_issued(self) -> None:
        command = KubectlRawTransport().command(PATH)
        self.assertEqual(command[-3:-1], ["get", "--raw"])
        for verb in ("apply", "delete", "patch", "scale", "create", "replace", "exec"):
            self.assertNotIn(verb, command)

    def test_paths_outside_the_readable_api_groups_are_rejected(self) -> None:
        transport = KubectlRawTransport()
        for path in (
            "/healthz",
            "/metrics",
            "/apis/rbac.authorization.k8s.io/v1/roles",
            "api/v1/pods",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.command(path)

    def test_stderr_is_classified(self) -> None:
        self.assertEqual(
            classify_stderr('Error from server (Forbidden): pods is forbidden: User "x"'),
            "forbidden",
        )
        self.assertEqual(
            classify_stderr("Error from server (NotFound): the server could not find it"),
            "not_found",
        )
        self.assertEqual(classify_stderr("Unable to connect to the server"), "unavailable")

    def test_failures_become_typed_transport_errors(self) -> None:
        cases = {
            "forbidden": "Error from server (Forbidden): denied",
            "not_found": "Error from server (NotFound): gone",
            "unavailable": "Unable to connect to the server",
        }
        for code, stderr in cases.items():
            with self.subTest(code=code):
                with patch(
                    "app.evidence.transport.subprocess.run",
                    return_value=completed(1, stderr=stderr),
                ):
                    with self.assertRaises(TransportError) as raised:
                        KubectlRawTransport().get(PATH)
                self.assertEqual(raised.exception.code, code)

    def test_timeouts_and_missing_binaries_are_unavailable(self) -> None:
        for error in (subprocess.TimeoutExpired(cmd="kubectl", timeout=1), FileNotFoundError("x")):
            with self.subTest(error=type(error).__name__):
                with patch("app.evidence.transport.subprocess.run", side_effect=error):
                    with self.assertRaises(TransportError) as raised:
                        KubectlRawTransport().get(PATH)
                self.assertEqual(raised.exception.code, "unavailable")

    def test_error_summaries_are_redacted_and_bounded(self) -> None:
        summary = summarize_stderr("failed token=abc123 " + "x" * 500 + "\nsecond line")
        self.assertNotIn("abc123", summary)
        self.assertNotIn("second line", summary)
        self.assertLessEqual(len(summary), 200)
        self.assertEqual(summarize_stderr(""), "no error output")


def named_document(kind: str, name: str) -> str:
    text = WORKLOAD_PATH.read_text(encoding="utf-8")
    for doc in re.split(r"(?m)^---\s*$", text):
        if not re.search(rf"(?m)^kind:\s*{re.escape(kind)}\s*$", doc):
            continue
        metadata_name = re.search(r"(?m)^  name:\s*([^\s]+)\s*$", doc)
        if metadata_name and metadata_name.group(1) == name:
            return doc
    raise AssertionError(f"{kind} {name} not found in workload.yaml")


class AgentIdentityContractTests(TestCase):
    def test_identity_matches_the_declared_service_account(self) -> None:
        document = named_document("ServiceAccount", "faultweld-agent-readonly")
        self.assertRegex(document, r"(?m)^  namespace: faultweld-demo$")
        self.assertEqual(
            AGENT_IDENTITY, "system:serviceaccount:faultweld-demo:faultweld-agent-readonly"
        )
        self.assertEqual(KIND_CONTEXT, "kind-faultweld")

    def test_agent_role_has_only_read_verbs_and_no_secrets(self) -> None:
        role = named_document("Role", "faultweld-agent-readonly")
        verbs: set[str] = set()
        for group in re.findall(r"(?m)^\s+verbs:\s*\[(.*?)\]", role):
            verbs |= {part.strip().strip("\"'") for part in group.split(",") if part.strip()}
        self.assertEqual(verbs, {"get", "list", "watch"})
        self.assertNotRegex(role, r"(?i)secrets?")

    def test_agent_role_covers_every_resource_the_adapter_reads(self) -> None:
        role = named_document("Role", "faultweld-agent-readonly")
        resources = " ".join(re.findall(r"(?m)^\s+resources:\s*\[(.*?)\]", role))
        for resource in ("pods", "services", "deployments"):
            self.assertIn(f'"{resource}"', resources)
