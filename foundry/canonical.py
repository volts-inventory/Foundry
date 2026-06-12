"""Canonical serialization + content addressing.

Every signature, attestation tuple, content identity, and cache key in
Foundry is computed over *canonical bytes* so that two logically-identical
records hash identically regardless of dict ordering, whitespace, or
platform. (architecture.md §3–4)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic byte encoding: sorted keys, compact separators, UTF-8.

    Rejects floats inside signed tuples by default-free strictness is NOT
    enforced here — callers that need float-free domains (e.g. budget cents)
    enforce it at the schema layer.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_encode_extra,
    ).encode("utf-8")


def _encode_extra(o: Any) -> Any:
    # NamedTuples (Seq) serialize as plain lists via json already; pydantic
    # models route through model_dump at the call site. Anything else is a bug.
    raise TypeError(f"non-canonical type in signed/hashed payload: {type(o)!r}")


def content_digest(obj: Any) -> str:
    """sha256 hex digest of the canonical encoding."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
