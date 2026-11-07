"""Canonical JSON shared by evidence records and action digests.

The encoding is the one the Kubernetes adapter has always used: sorted keys, no
insignificant whitespace, and ASCII escapes. It lives in its own module so agent, policy
and gateway code can hash a payload without importing the Kubernetes adapter and, through
it, ``subprocess``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import JsonValue


def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
