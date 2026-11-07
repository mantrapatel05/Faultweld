from __future__ import annotations

import hashlib
from datetime import timedelta
from unittest import TestCase

from pydantic import JsonValue

from app.agent.errors import AgentOutputError
from app.agent.identity import (
    CLUSTER_ID,
    DEPLOYMENT_KIND,
    DEPLOYMENT_NAME,
    DEPLOYMENT_UID,
    ENVIRONMENT_ID,
    NAMESPACE,
)
from app.agent.ids import SequentialIdFactory
from app.agent.proposal import (
    ATTEMPT_LIMIT_PLACEHOLDER,
    EXPECTED_POSTCONDITIONS,
    POLICY_BUNDLE_DIGEST_PLACEHOLDER,
    POLICY_VERSION_PLACEHOLDER,
    PROPOSAL_TTL,
    build_proposal,
    canonical_parameter_digest,
    parse_proposal,
    proposal_digest,
)
from app.agent.reasons import (
    INVALID_MODEL_OUTPUT,
    INVALID_PARAMETERS,
    UNKNOWN_EVIDENCE_ID,
    UNSUPPORTED_ACTION,
)
from app.domain.schemas import ActionProposal, ActionType, ScaleDeploymentArgs
from app.evidence.canonical import canonical_json

from .agent_support import CONTEXT, FIXED_NOW, proposal_output, scenario_snapshot, snapshot_item

EVIDENCE_ID = snapshot_item().record.evidence_id
SNAPSHOT = scenario_snapshot("healthy-replica-loss")


def build(
    output: dict[str, JsonValue],
    *,
    ids: SequentialIdFactory | None = None,
    evidence_ids: tuple[str, ...] = (EVIDENCE_ID,),
) -> ActionProposal | None:
    draft = parse_proposal(output)
    return build_proposal(
        draft,
        context=CONTEXT,
        snapshot=SNAPSHOT,
        observed_at=FIXED_NOW,
        supplied_evidence_ids=evidence_ids,
        ids=ids or SequentialIdFactory(),
    )


def require_proposal(output: dict[str, JsonValue]) -> ActionProposal:
    proposal = build(output)
    assert proposal is not None
    return proposal


class ServerFilledProposalTests(TestCase):
    def test_a_scale_proposal_is_filled_entirely_by_the_server(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID], parameters={"replicas": 1}))
        self.assertEqual(proposal.schema_version, "0.1")
        self.assertEqual(proposal.proposal_id, "proposal-1")
        self.assertEqual(proposal.incident_id, CONTEXT.incident_id)
        self.assertEqual(proposal.run_id, CONTEXT.run_id)
        self.assertEqual(proposal.principal_id, f"agent-run-{CONTEXT.run_id}")
        self.assertEqual(proposal.environment_id, ENVIRONMENT_ID)
        self.assertEqual(proposal.action_type, ActionType.SCALE_DEPLOYMENT)
        self.assertEqual(proposal.created_at, FIXED_NOW)
        self.assertEqual(proposal.state_observed_at, FIXED_NOW)
        self.assertEqual(proposal.expected_state_fingerprint, SNAPSHOT.state_fingerprint)
        self.assertEqual(proposal.policy_version, POLICY_VERSION_PLACEHOLDER)
        self.assertEqual(proposal.policy_bundle_digest, POLICY_BUNDLE_DIGEST_PLACEHOLDER)
        self.assertEqual(proposal.source_evidence_ids, (EVIDENCE_ID,))
        self.assertEqual(proposal.attempt_limit, ATTEMPT_LIMIT_PLACEHOLDER)
        self.assertEqual(proposal.expires_at, FIXED_NOW + timedelta(minutes=15))
        self.assertEqual(proposal.expires_at - proposal.created_at, PROPOSAL_TTL)

    def test_the_target_is_server_resolved_and_never_model_chosen(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        target = proposal.arguments.target
        self.assertEqual(target.cluster_id, CLUSTER_ID)
        self.assertEqual(target.namespace, NAMESPACE)
        self.assertEqual(target.kind, DEPLOYMENT_KIND)
        self.assertEqual(target.name, DEPLOYMENT_NAME)
        self.assertEqual(target.uid, DEPLOYMENT_UID)

    def test_typed_arguments_carry_the_scaled_replica_count(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID], parameters={"replicas": 1}))
        arguments = proposal.arguments
        self.assertIsInstance(arguments, ScaleDeploymentArgs)
        assert isinstance(arguments, ScaleDeploymentArgs)
        self.assertEqual(arguments.replicas, 1)

    def test_restart_is_typed_and_has_its_own_postconditions(self) -> None:
        proposal = require_proposal(
            proposal_output(
                [EVIDENCE_ID],
                action_type="restart_deployment",
                parameters={},
            )
        )
        self.assertEqual(proposal.action_type, ActionType.RESTART_DEPLOYMENT)
        self.assertEqual(
            proposal.expected_postconditions,
            EXPECTED_POSTCONDITIONS[ActionType.RESTART_DEPLOYMENT],
        )

    def test_no_action_is_not_a_proposal(self) -> None:
        self.assertIsNone(
            build(proposal_output([EVIDENCE_ID], action_type=None, parameters={}))
        )

    def test_a_scale_above_the_simulation_limit_is_left_for_policy(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID], parameters={"replicas": 5}))
        arguments = proposal.arguments
        assert isinstance(arguments, ScaleDeploymentArgs)
        self.assertEqual(arguments.replicas, 5)

    def test_the_same_draft_and_ids_produce_the_same_proposal(self) -> None:
        output = proposal_output([EVIDENCE_ID])
        first = require_proposal(output)
        second = build(output, ids=SequentialIdFactory())
        assert second is not None
        self.assertEqual(first.model_dump_json(), second.model_dump_json())

    def test_the_idempotency_key_is_derived_from_the_proposal_id(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        expected = "idem-" + hashlib.sha256(proposal.proposal_id.encode()).hexdigest()
        self.assertEqual(proposal.idempotency_key, expected)


class ProposalDigestTests(TestCase):
    def test_the_parameter_digest_matches_its_definition(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        material: dict[str, JsonValue] = {
            "action_type": proposal.action_type.value,
            "arguments": proposal.arguments.model_dump(mode="json"),
        }
        expected = hashlib.sha256(canonical_json(material).encode()).hexdigest()
        self.assertEqual(proposal.canonical_parameter_digest, expected)
        self.assertEqual(
            proposal.canonical_parameter_digest,
            canonical_parameter_digest(proposal.action_type, proposal.arguments),
        )

    def test_the_proposal_digest_matches_its_definition(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        expected = hashlib.sha256(
            canonical_json(proposal.model_dump(mode="json")).encode()
        ).hexdigest()
        self.assertEqual(proposal_digest(proposal), expected)

    def test_key_order_does_not_change_the_digest(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        dumped = proposal.model_dump(mode="json")
        reordered = dict(reversed(list(dumped.items())))
        self.assertEqual(
            hashlib.sha256(canonical_json(dumped).encode()).hexdigest(),
            hashlib.sha256(canonical_json(reordered).encode()).hexdigest(),
        )

    def test_changing_any_single_field_changes_the_proposal_digest(self) -> None:
        proposal = require_proposal(proposal_output([EVIDENCE_ID]))
        original = proposal_digest(proposal)
        for change in (
            {"attempt_limit": 2},
            {"proposal_id": "proposal-other"},
            {"source_evidence_ids": ()},
        ):
            with self.subTest(change=change):
                changed = proposal.model_copy(update=change)
                self.assertNotEqual(proposal_digest(changed), original)


class InvalidProposalTests(TestCase):
    def assert_rejected(self, output: dict[str, JsonValue], reason: str) -> None:
        with self.assertRaises(AgentOutputError) as caught:
            build(output)
        self.assertEqual(caught.exception.reason, reason)

    def test_rollback_is_unsupported_in_lite(self) -> None:
        self.assert_rejected(
            proposal_output(
                [EVIDENCE_ID],
                action_type="rollback_deployment",
                parameters={"revision": "2"},
            ),
            UNSUPPORTED_ACTION,
        )

    def test_an_unknown_action_string_is_rejected(self) -> None:
        self.assert_rejected(
            proposal_output([EVIDENCE_ID], action_type="delete_deployment"),
            INVALID_MODEL_OUTPUT,
        )

    def test_negative_replicas_are_rejected_as_parameters(self) -> None:
        self.assert_rejected(
            proposal_output([EVIDENCE_ID], parameters={"replicas": -1}),
            INVALID_PARAMETERS,
        )

    def test_non_integer_replicas_are_rejected_as_parameters(self) -> None:
        for value in (1.5, True, "1", None):
            with self.subTest(value=value):
                self.assert_rejected(
                    proposal_output([EVIDENCE_ID], parameters={"replicas": value}),
                    INVALID_PARAMETERS,
                )

    def test_unknown_scale_parameters_are_rejected(self) -> None:
        self.assert_rejected(
            proposal_output(
                [EVIDENCE_ID],
                parameters={"replicas": 1, "image": "evil:latest"},
            ),
            INVALID_PARAMETERS,
        )

    def test_restart_does_not_accept_parameters(self) -> None:
        self.assert_rejected(
            proposal_output(
                [EVIDENCE_ID],
                action_type="restart_deployment",
                parameters={"replicas": 1},
            ),
            INVALID_PARAMETERS,
        )

    def test_an_unknown_evidence_id_is_rejected(self) -> None:
        self.assert_rejected(
            proposal_output(["ev-made-up"]),
            UNKNOWN_EVIDENCE_ID,
        )

    def test_an_oversized_rationale_is_rejected(self) -> None:
        self.assert_rejected(
            proposal_output([EVIDENCE_ID], rationale="x" * 501),
            INVALID_MODEL_OUTPUT,
        )

    def test_an_empty_rationale_is_rejected(self) -> None:
        self.assert_rejected(proposal_output([EVIDENCE_ID], rationale=""), INVALID_MODEL_OUTPUT)

    def test_server_owned_fields_cannot_be_supplied_by_the_model(self) -> None:
        fields = (
            "target",
            "namespace",
            "principal_id",
            "expires_at",
            "expected_state_fingerprint",
            "canonical_parameter_digest",
            "idempotency_key",
        )
        for field in fields:
            with self.subTest(field=field):
                output = proposal_output([EVIDENCE_ID])
                output[field] = "model-chosen"
                self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_any_unknown_proposal_field_is_rejected(self) -> None:
        output = proposal_output([EVIDENCE_ID])
        output["approved"] = True
        self.assert_rejected(output, INVALID_MODEL_OUTPUT)
