from datetime import UTC, datetime, timedelta
from unittest import TestCase

from pydantic import ValidationError

from app.domain.schemas import (
    ActionAttempt,
    ActionOutcome,
    ActionPreflightSnapshot,
    ActionProposal,
    ActionType,
    AgentRun,
    AgentRunStatus,
    Approval,
    ApprovalDecision,
    Diagnosis,
    DiagnosisHypothesis,
    Evidence,
    EvidenceFreshness,
    EvidenceSourceType,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    PolicyConstraints,
    PolicyDecision,
    PolicyDecisionType,
    ResourceRef,
    RestartDeploymentArgs,
    RollbackDeploymentArgs,
    RunBudget,
    ScaleDeploymentArgs,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)

DIGEST = "a" * 64
OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def resource() -> ResourceRef:
    return ResourceRef(
        schema_version="0.1",
        cluster_id="kind-local",
        namespace="trust-lab",
        kind="Deployment",
        name="checkout",
        uid="resource-uid-1",
    )


def incident() -> Incident:
    return Incident(
        schema_version="0.1",
        incident_id="incident-1",
        scenario_id="scenario-1",
        environment_id="local",
        status=IncidentStatus.INVESTIGATING,
        normalized_alert=NormalizedAlert(
            schema_version="0.1",
            alert_id="alert-1",
            source="synthetic",
            summary="Synthetic checkout alert",
            observed_at=OBSERVED_AT,
        ),
        opened_at=OBSERVED_AT,
        updated_at=OBSERVED_AT,
        current_run_id="run-1",
    )


def proposal() -> ActionProposal:
    return ActionProposal(
        schema_version="0.1",
        proposal_id="proposal-1",
        incident_id="incident-1",
        run_id="run-1",
        principal_id="agent-run-1",
        environment_id="local",
        action_type=ActionType.RESTART_DEPLOYMENT,
        arguments=RestartDeploymentArgs(schema_version="0.1", target=resource()),
        created_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(minutes=15),
        expected_state_fingerprint=DIGEST,
        state_observed_at=OBSERVED_AT,
        policy_version="policy-1",
        policy_bundle_digest=DIGEST,
        source_evidence_ids=("evidence-1",),
        expected_postconditions=("deployment-ready",),
        canonical_parameter_digest=DIGEST,
        idempotency_key="attempt-key-1",
        attempt_limit=1,
    )


class DomainSchemaTests(TestCase):
    def test_versioned_records_round_trip(self) -> None:
        incident_record = incident()
        run_record = AgentRun(
            schema_version="0.1",
            run_id="run-1",
            incident_id="incident-1",
            status=AgentRunStatus.RUNNING,
            workflow_version="workflow-1",
            model_provider_id="provider-1",
            model_id="model-1",
            prompt_config_revision="config-1",
            started_at=OBSERVED_AT,
            budgets=RunBudget(
                schema_version="0.1",
                max_wall_clock_seconds=900,
                max_model_calls=10,
                max_tool_calls=20,
                max_tokens=10000,
                max_cost_usd=1,
            ),
            trace_id="trace-1",
        )
        evidence_record = Evidence(
            schema_version="0.1",
            evidence_id="evidence-1",
            incident_id="incident-1",
            run_id="run-1",
            source_type=EvidenceSourceType.KUBERNETES,
            source_resource="Deployment/trust-lab/checkout",
            query="metadata.name=checkout",
            observed_at=OBSERVED_AT,
            freshness=EvidenceFreshness.FRESH,
            content_hash=DIGEST,
            payload_reference="evidence-store/evidence-1",
        )
        diagnosis_record = Diagnosis(
            schema_version="0.1",
            diagnosis_id="diagnosis-1",
            run_id="run-1",
            hypotheses=(
                DiagnosisHypothesis(
                    schema_version="0.1",
                    hypothesis_id="hypothesis-1",
                    statement="The deployment is unavailable",
                    confidence=0.8,
                    supporting_evidence_ids=("evidence-1",),
                ),
            ),
            confidence=0.8,
            is_sufficient=True,
        )
        proposal_record = proposal()
        decision_record = PolicyDecision(
            schema_version="0.1",
            decision_id="decision-1",
            proposal_id="proposal-1",
            decision=PolicyDecisionType.ALLOW,
            reason_codes=("local-demo-allow",),
            policy_version="policy-1",
            policy_bundle_digest=DIGEST,
            constraints=PolicyConstraints(
                schema_version="0.1",
                allowed_namespace="trust-lab",
                allowed_resource="Deployment/trust-lab/checkout",
                max_attempts=1,
            ),
        )
        approval_record = Approval(
            schema_version="0.1",
            approval_id="approval-1",
            proposal_id="proposal-1",
            proposal_digest=DIGEST,
            approver_principal_id="human-1",
            approver_role="reviewer",
            decision=ApprovalDecision.APPROVE,
            decided_at=OBSERVED_AT,
            expires_at=OBSERVED_AT + timedelta(minutes=10),
            policy_version="policy-1",
            state_fingerprint=DIGEST,
        )
        attempt_record = ActionAttempt(
            schema_version="0.1",
            attempt_id="attempt-1",
            proposal_id="proposal-1",
            idempotency_key="attempt-key-1",
            attempt_number=1,
            preflight=ActionPreflightSnapshot(
                schema_version="0.1",
                target=resource(),
                state_fingerprint=DIGEST,
                observed_at=OBSERVED_AT,
                proposal_digest=DIGEST,
            ),
            authorization_decision_id="decision-1",
            operation_started_at=OBSERVED_AT,
            operation_finished_at=OBSERVED_AT + timedelta(seconds=1),
            outcome=ActionOutcome.EXECUTED,
            after_resource_version="revision-2",
            trace_id="trace-1",
        )
        verification_record = VerificationResult(
            schema_version="0.1",
            verification_id="verification-1",
            incident_id="incident-1",
            attempt_id="attempt-1",
            status=VerificationStatus.RECOVERED,
            checks=(
                VerificationCheck(
                    schema_version="0.1",
                    check_id="deployment-ready",
                    source="kubernetes",
                    observed_at=OBSERVED_AT + timedelta(seconds=2),
                    passed=True,
                    observed_value="3/3",
                    expected_value="3/3",
                ),
            ),
            observed_at=OBSERVED_AT + timedelta(seconds=2),
        )

        for record in (
            incident_record,
            run_record,
            evidence_record,
            diagnosis_record,
            proposal_record,
            decision_record,
            approval_record,
            attempt_record,
            verification_record,
        ):
            self.assertEqual(record.schema_version, "0.1")
            self.assertEqual(type(record).model_validate_json(record.model_dump_json()), record)

    def test_serialization_is_stable_across_input_key_order(self) -> None:
        first_payload = proposal().model_dump(mode="json")
        second_payload = dict(reversed(list(first_payload.items())))

        first_record = ActionProposal.model_validate(first_payload)
        second_record = ActionProposal.model_validate(second_payload)

        self.assertEqual(first_record.model_dump_json(), second_record.model_dump_json())

    def test_unknown_action_is_rejected(self) -> None:
        payload = proposal().model_dump(mode="json")
        payload["action_type"] = "execute_shell"

        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(payload)

    def test_arbitrary_action_fields_are_rejected(self) -> None:
        payload = proposal().model_dump(mode="json")
        payload["command"] = "remove all pods"

        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(payload)

    def test_action_type_must_match_typed_arguments(self) -> None:
        payload = proposal().model_dump(mode="json")
        payload["action_type"] = ActionType.SCALE_DEPLOYMENT.value

        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(payload)

    def test_invalid_state_and_version_are_rejected(self) -> None:
        run_payload = {
            "run_id": "run-1",
            "incident_id": "incident-1",
            "status": "deleted",
            "workflow_version": "workflow-1",
            "model_provider_id": "provider-1",
            "model_id": "model-1",
            "prompt_config_revision": "config-1",
            "started_at": OBSERVED_AT,
            "budgets": {},
        }
        with self.assertRaises(ValidationError):
            AgentRun.model_validate(run_payload)

        resource_payload = resource().model_dump(mode="json")
        del resource_payload["schema_version"]
        with self.assertRaises(ValidationError):
            ResourceRef.model_validate(resource_payload)

    def test_recovered_verification_requires_passing_checks(self) -> None:
        payload = {
            "schema_version": "0.1",
            "verification_id": "verification-1",
            "incident_id": "incident-1",
            "attempt_id": "attempt-1",
            "status": VerificationStatus.RECOVERED.value,
            "checks": [
                {
                    "schema_version": "0.1",
                    "check_id": "deployment-ready",
                    "source": "kubernetes",
                    "observed_at": OBSERVED_AT,
                    "passed": False,
                }
            ],
            "observed_at": OBSERVED_AT,
        }

        with self.assertRaises(ValidationError):
            VerificationResult.model_validate(payload)

    def test_typed_action_variants_round_trip(self) -> None:
        for arguments in (
            RestartDeploymentArgs(schema_version="0.1", target=resource()),
            RollbackDeploymentArgs(
                schema_version="0.1",
                target=resource(),
                revision="revision-1",
            ),
            ScaleDeploymentArgs(schema_version="0.1", target=resource(), replicas=3),
        ):
            payload = {
                "schema_version": "0.1",
                "proposal_id": "proposal-1",
                "incident_id": "incident-1",
                "run_id": "run-1",
                "principal_id": "agent-run-1",
                "environment_id": "local",
                "action_type": arguments.action_type.value,
                "arguments": arguments.model_dump(mode="json"),
                "created_at": OBSERVED_AT,
                "expires_at": OBSERVED_AT + timedelta(minutes=15),
                "expected_state_fingerprint": DIGEST,
                "state_observed_at": OBSERVED_AT,
                "policy_version": "policy-1",
                "policy_bundle_digest": DIGEST,
                "source_evidence_ids": ("evidence-1",),
                "expected_postconditions": ("deployment-ready",),
                "canonical_parameter_digest": DIGEST,
                "idempotency_key": "attempt-key-1",
                "attempt_limit": 1,
            }
            self.assertEqual(ActionProposal.model_validate(payload).arguments, arguments)
