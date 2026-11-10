"""Ablation driver: run labelled arms over identical cases and repeats.

Two ablations only: single-shot vs agentic for one model, and two model sizes in agentic
mode. No arm disables a safety control. Summaries live in the report module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.evals.live import (
    LiveCase,
    LiveConfig,
    LiveOutcome,
    Mode,
    ProviderFactory,
    run_live,
)
from app.policy.config import PolicyConfig


@dataclass(frozen=True)
class Arm:
    label: str
    config: LiveConfig
    provider_factory: ProviderFactory


@dataclass(frozen=True)
class ArmResult:
    label: str
    mode: Mode
    model_id: str
    outcome: LiveOutcome


def mode_arms(base: LiveConfig, factory: ProviderFactory) -> tuple[Arm, ...]:
    """Single-shot vs agentic: same model, same k, same cases."""
    return tuple(
        Arm(f"{mode}", base.model_copy(update={"mode": mode}), factory)
        for mode in ("single-shot", "agentic")
    )


def model_arms(
    base: LiveConfig, factories: Sequence[tuple[str, ProviderFactory]]
) -> tuple[Arm, ...]:
    """Two (or more) model sizes in agentic mode; model ids come from configuration."""
    return tuple(
        Arm(
            model_id,
            base.model_copy(update={"mode": "agentic", "model_id": model_id}),
            factory,
        )
        for model_id, factory in factories
    )


def run_ablation(
    arms: Sequence[Arm],
    cases: tuple[LiveCase, ...],
    *,
    policy_config: PolicyConfig | None = None,
) -> tuple[ArmResult, ...]:
    """Run every arm over the same cases. Labels must be unique."""
    labels = [arm.label for arm in arms]
    if len(set(labels)) != len(labels):
        raise ValueError("ablation arm labels must be unique")
    repeats = {arm.config.repeats for arm in arms}
    if len(repeats) > 1:
        raise ValueError("ablation arms must use the same number of repeats")
    return tuple(
        ArmResult(
            arm.label,
            arm.config.mode,
            arm.config.model_id,
            run_live(cases, arm.config, arm.provider_factory, policy_config=policy_config),
        )
        for arm in arms
    )
