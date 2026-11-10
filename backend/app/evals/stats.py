"""Pure statistics helpers for live evaluation reports. No I/O, no randomness."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

WILSON_Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = WILSON_Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Refuses n == 0."""
    if n <= 0:
        raise ValueError("a proportion needs n > 0 to have an interval")
    if successes < 0 or successes > n:
        raise ValueError("successes must be between 0 and n")
    phat = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (phat + z2 / (2 * n)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


@dataclass(frozen=True)
class Proportion:
    successes: int
    n: int

    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.successes, self.n)

    def format(self) -> str:
        """``k/n (p%, 95% Wilson interval lo%-hi%)``. Always shows n and an interval."""
        if self.n == 0:
            return "n=0 (no runs, no estimate)"
        low, high = self.interval()
        rate = 100 * self.successes / self.n
        return (
            f"{self.successes}/{self.n} ({rate:.1f}%, "
            f"95% Wilson interval {100 * low:.1f}%-{100 * high:.1f}%)"
        )


def pass_hat_k(groups: Iterable[Sequence[bool]]) -> tuple[int, int]:
    """pass^k: a group passes only if it is non-empty and every one of its runs passed.

    Returns ``(groups_passed, groups_total)``. An empty group is a failure, not a pass.
    """
    passed = total = 0
    for group in groups:
        total += 1
        if len(group) > 0 and all(group):
            passed += 1
    return passed, total


@dataclass(frozen=True)
class Spread:
    n: int
    mean: float
    minimum: float
    maximum: float


def spread(values: Sequence[float]) -> Spread | None:
    """Mean and range over all values (each run weighted equally). None when empty."""
    if not values:
        return None
    return Spread(
        n=len(values),
        mean=sum(values) / len(values),
        minimum=min(values),
        maximum=max(values),
    )


def pooled(groups: Iterable[tuple[int, int]]) -> Proportion:
    """Pool (successes, n) pairs by summing counts, so every run has equal weight."""
    successes = n = 0
    for group_successes, group_n in groups:
        successes += group_successes
        n += group_n
    return Proportion(successes, n)
