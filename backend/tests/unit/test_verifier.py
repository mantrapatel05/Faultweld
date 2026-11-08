from __future__ import annotations

from dataclasses import replace
from unittest import TestCase

from app.agent.ids import SequentialIdFactory
from app.domain.schemas import VerificationResult, VerificationStatus
from app.gateway.verifier import VerifierConfig, verify
from app.scenarios.health import HealthObservation
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster, SimSnapshot

from .test_policy import FIXED_NOW


class VerifierTests(TestCase):
    def run_verifier(
        self, cluster: SimCluster, scenario_id: str, config: VerifierConfig | None = None
    ) -> VerificationResult:
        return verify(
            load_scenarios()[scenario_id],
            cluster.source,
            cluster.tick,
            config or VerifierConfig(),
            incident_id="incident-1",
            attempt_id="attempt-1",
            clock=lambda: FIXED_NOW,
            ids=SequentialIdFactory(),
        )

    def test_scaled_replica_loss_requires_consecutive_healthy_observations(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(load_scenarios()["healthy-replica-loss"])
        cluster.apply_scale(1, cluster.snapshot().resource_version)
        result = self.run_verifier(cluster, "healthy-replica-loss")
        self.assertEqual(result.status, VerificationStatus.RECOVERED)
        self.assertTrue(all(check.passed is True for check in result.checks))

    def test_restart_on_bad_probe_is_not_recovery(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(load_scenarios()["readiness-regression"])
        cluster.apply_restart(cluster.snapshot().resource_version)
        result = self.run_verifier(cluster, "readiness-regression")
        self.assertEqual(result.status, VerificationStatus.NOT_RECOVERED)
        self.assertTrue(any(check.passed is False for check in result.checks))

    def test_one_healthy_sample_does_not_meet_the_consecutive_requirement(self) -> None:
        result = self.run_verifier(
            SimCluster(),
            "healthy-replica-loss",
            VerifierConfig(consecutive_healthy=3, window_ticks=1),
        )
        self.assertEqual(result.status, VerificationStatus.NOT_RECOVERED)
        self.assertEqual(result.checks[-1].check_id, "consecutive-healthy")

    def test_source_failure_returns_unknown_with_all_checks_unknown(self) -> None:
        class BrokenSource:
            def snapshot(self) -> SimSnapshot:
                raise OSError("offline")

        result = verify(
            load_scenarios()["healthy-replica-loss"],
            BrokenSource(),
            lambda _ticks: None,
            VerifierConfig(),
            incident_id="incident-1",
            attempt_id="attempt-1",
            clock=lambda: FIXED_NOW,
            ids=SequentialIdFactory(),
        )
        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertTrue(all(check.passed is None for check in result.checks))

    def test_contradictory_snapshot_returns_unknown(self) -> None:
        cluster = SimCluster()

        class ContradictorySource:
            def snapshot(self) -> SimSnapshot:
                current = cluster.snapshot()
                observation = HealthObservation(
                    desired_replicas=0, available_replicas=1, ready_replicas=1
                )
                return replace(current, observation=observation)

        result = verify(
            load_scenarios()["healthy-replica-loss"],
            ContradictorySource(),
            lambda _ticks: None,
            VerifierConfig(),
            incident_id="incident-1",
            attempt_id="attempt-1",
            clock=lambda: FIXED_NOW,
            ids=SequentialIdFactory(),
        )
        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual({check.passed for check in result.checks}, {None})

    def test_tick_moving_backwards_returns_unknown(self) -> None:
        cluster = SimCluster()

        class BackwardsSource:
            calls = 0

            def snapshot(self) -> SimSnapshot:
                self.calls += 1
                tick = 1 if self.calls == 1 else 0
                return replace(cluster.snapshot(), tick=tick)

        result = verify(
            load_scenarios()["healthy-replica-loss"],
            BackwardsSource(),
            lambda _ticks: None,
            VerifierConfig(),
            incident_id="incident-1",
            attempt_id="attempt-1",
            clock=lambda: FIXED_NOW,
            ids=SequentialIdFactory(),
        )
        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual(result.reason_code, "tick_went_backwards")
        self.assertTrue(all(check.passed is None for check in result.checks))
