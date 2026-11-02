"""Phase-1 2b/2c: real materialization seam + dynamic judgment bindings.

`run()` keeps the deterministic synthetic doubles ONLY for the `mock` smoke
backend. A real backend resolves straight off the real CAS (so the agents'
authored bodies + the adapter's real FeatureDiff are what the gates run) and
pins NO synthetic judgment label (the reviewer binds the real subject + tree).

These are deterministic (no live backend): they pin the pure selection helpers
and the reviewer's tree-sha fallback, and assert the `mock` path is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from foundry.cli import (
    _DIFF_BODY_URI,
    _judgment_anchors,
    _materialization_seam,
)
from foundry.durability.artifact_store import ArtifactStore


# ---- 2b: materialization seam -----------------------------------------------


def test_mock_uses_synthetic_doubles() -> None:
    store = ArtifactStore()
    resolve, resolve_blob = _materialization_seam(False, store=store, diff_body="X")
    # The synthetic resolve_blob returns the diff_body for the synthetic uri.
    assert resolve_blob(_DIFF_BODY_URI) == b"X"
    # And it is NOT the store's own bound method (a real-store call would KeyError).
    assert resolve_blob is not store.resolve_blob


def test_real_backend_resolves_off_the_store() -> None:
    store = ArtifactStore()
    resolve, resolve_blob = _materialization_seam(True, store=store, diff_body="X")
    # Real path delegates straight to the CAS (same underlying functions, no
    # synthetic override). Bound methods aren't identity-equal, so compare funcs.
    assert resolve.__func__ is ArtifactStore.resolve  # type: ignore[attr-defined]
    assert resolve_blob.__func__ is ArtifactStore.resolve_blob  # type: ignore[attr-defined]
    # And it does NOT special-case the synthetic diff uri — the real seam has no
    # canned body, so an unknown uri raises rather than returning a fake diff.
    with pytest.raises((KeyError, Exception)):
        resolve_blob(_DIFF_BODY_URI)


# ---- 2c: judgment-binding anchors -------------------------------------------


@pytest.mark.parametrize(
    ("real", "anchors"),
    [
        (False, {"subject_artifact_uri": _DIFF_BODY_URI, "state_sha": "integrated-sha"}),
        # Nothing pinned => the reviewer falls back to the REAL subject + tree (state).
        (True, {}),
    ],
    ids=["mock", "real"],
)
def test_judgment_anchors(real: bool, anchors: dict[str, str]) -> None:
    assert _judgment_anchors(real) == anchors


# ---- 2c: reviewer's real tree-sha fallback ----------------------------------


def _gate(name: str, run_against: str, seq: int) -> Any:
    return SimpleNamespace(name=name, run_against=run_against, seq=seq)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        # Newest-by-seq wins regardless of channel order.
        ({"gates": (_gate("test", "tree-B", 2), _gate("build", "tree-A", 1))}, "tree-B"),
        ({"gates": (_gate("build", "tree-A", 1), _gate("test", "tree-B", 2))}, "tree-B"),
        # A newer verdict with an empty run_against is ignored.
        ({"gates": (_gate("build", "tree-A", 1), _gate("review", "", 5))}, "tree-A"),
        ({"gates": (_gate("review", "", 5),)}, None),
        ({"gates": ()}, None),
        ({}, None),
    ],
)
def test_attested_tree_sha(state: dict[str, Any], expected: str | None) -> None:
    from foundry.agents.quality.reviewer_critic import _attested_tree_sha

    assert _attested_tree_sha(state) == expected
