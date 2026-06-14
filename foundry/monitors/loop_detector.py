"""Semantic loop detector. (architecture.md §7, governance.md §3)

Keys on ``(criterion_or_task_id, gate_name, failure_signature)`` where the
failure signature is a content digest over the **sorted, normalized failing
test ids / error classes — never diff bytes** (arch §7): cosmetically
different regenerations of the same stuck attempt hash identically and are
detected; a normalized-diff token-set Jaccard similarity is a *secondary,
advisory-only* signal — it can flag, it can never be the trip itself.

Deterministic — no LLM (gov §3).
"""

from __future__ import annotations

from collections.abc import Iterable

from foundry.canonical import content_digest
from foundry.monitors import Trip, TripKind
from foundry.schemas.base import FrozenModel

DEFAULT_MAX_REPEATS = 3
DEFAULT_SIMILARITY_THRESHOLD = 0.85


def normalize_failure_id(raw: str) -> str:
    """Whitespace-collapsed failing-test-id / error-class normalization."""
    return " ".join(raw.split())


def failure_signature(failing_ids: Iterable[str]) -> str:
    """content_digest of the sorted, de-duplicated, normalized failure set.

    Deliberately ignores diff bytes, ordering, duplication, and whitespace —
    the *semantic* failure is the signature (arch §7).
    """
    normalized = sorted({normalize_failure_id(f) for f in failing_ids})
    return content_digest(normalized)


def token_set_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity over whitespace-split normalized diffs."""
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class LoopVerdict(FrozenModel):
    """Result of one `observe` call."""

    key: tuple[str, str, str]  # (criterion_or_task_id, gate_name, failure_signature)
    repeats: int  # observations of this exact semantic signature
    tripped: bool
    trip: Trip | None = None
    # Secondary signal — ADVISORY ONLY, never the trip (arch §7):
    diff_similarity: float | None = None
    similarity_flag: bool = False


class SemanticLoopDetector:
    """Trips after `max_repeats` identical semantic signatures (default 3)."""

    def __init__(
        self,
        max_repeats: int = DEFAULT_MAX_REPEATS,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        if max_repeats < 2:
            raise ValueError("max_repeats must be >= 2")
        self.max_repeats = max_repeats
        self.similarity_threshold = similarity_threshold
        self._repeats: dict[tuple[str, str, str], int] = {}
        # last normalized diff per (criterion/task, gate) for the advisory signal:
        self._last_diff: dict[tuple[str, str], str] = {}

    def observe(
        self,
        criterion_or_task_id: str,
        gate_name: str,
        failing_ids: Iterable[str],
        diff_text: str | None = None,
    ) -> LoopVerdict:
        sig = failure_signature(failing_ids)
        key = (criterion_or_task_id, gate_name, sig)
        repeats = self._repeats.get(key, 0) + 1
        self._repeats[key] = repeats

        similarity: float | None = None
        similarity_flag = False
        if diff_text is not None:
            diff_key = (criterion_or_task_id, gate_name)
            prev = self._last_diff.get(diff_key)
            if prev is not None:
                similarity = token_set_jaccard(prev, diff_text)
                similarity_flag = similarity >= self.similarity_threshold
            self._last_diff[diff_key] = diff_text

        tripped = repeats >= self.max_repeats
        trip: Trip | None = None
        if tripped:
            trip = Trip(
                monitor="loop_detector",
                kind=TripKind.LOOP,
                rule="semantic-signature-repeat",
                reason=(
                    f"signature for ({criterion_or_task_id!r}, {gate_name!r}) repeated "
                    f"{repeats}x (bound {self.max_repeats})"
                ),
                matched=(criterion_or_task_id, gate_name, sig),
            )
        return LoopVerdict(
            key=key,
            repeats=repeats,
            tripped=tripped,
            trip=trip,
            diff_similarity=similarity,
            similarity_flag=similarity_flag,
        )
