"""Deterministic and production ID generation for agent records."""

from __future__ import annotations

import uuid
from collections.abc import Callable

type IdFactory = Callable[[str], str]


def uuid_id(prefix: str) -> str:
    """A unique, prefixed ID for real runs."""
    return f"{prefix}-{uuid.uuid4().hex}"


class SequentialIdFactory:
    """Per-prefix counters, so tests and reproducible demos get stable IDs."""

    def __init__(self) -> None:
        self._next: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        value = self._next.get(prefix, 0) + 1
        self._next[prefix] = value
        return f"{prefix}-{value}"
