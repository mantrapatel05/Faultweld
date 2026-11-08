"""Typed write operations for the in-memory simulation adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PreconditionFailed(RuntimeError):
    """The live resource version no longer matches the checked version."""


class UnsupportedOperation(RuntimeError):
    """The Lite writer intentionally does not implement this action."""


@dataclass(frozen=True)
class RestartOperation:
    expected_resource_version: int


@dataclass(frozen=True)
class ScaleOperation:
    replicas: int
    expected_resource_version: int


@dataclass(frozen=True)
class RollbackOperation:
    revision: str
    expected_resource_version: int


type WriteOperation = RestartOperation | ScaleOperation | RollbackOperation


@dataclass(frozen=True)
class WriteResult:
    operation: WriteOperation
    before_resource_version: int
    after_resource_version: int
    state_fingerprint: str


class ClusterWriter(Protocol):
    def apply(self, operation: WriteOperation) -> WriteResult: ...


__all__ = [
    "ClusterWriter",
    "PreconditionFailed",
    "RestartOperation",
    "RollbackOperation",
    "ScaleOperation",
    "UnsupportedOperation",
    "WriteOperation",
    "WriteResult",
]
