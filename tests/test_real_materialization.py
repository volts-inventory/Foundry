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
    import pytest

    with pytest.raises((KeyError, Exception)):
        resolve_blob(_DIFF_BODY_URI)


# ---- 2c: judgment-binding anchors -------------------------------------------


def test_mock_pins_synthetic_anchors() -> None:
    anchors = _judgment_anchors(False)
    assert anchors == {
        "subject_artifact_uri": _DIFF_BODY_URI,
        "state_sha": "integrated-sha",
    }


def test_real_backend_pins_no_anchors() -> None:
    # Nothing pinned ⇒ the reviewer falls back to the REAL subject + tree (state).
    assert _judgment_anchors(True) == {}


# ---- 2c: reviewer's real tree-sha fallback ----------------------------------


def _gate(name: str, run_against: str, seq_local: int) -> Any:
    return SimpleNamespace(
        name=name,
        run_against=run_against,
        seq=SimpleNamespace(local=seq_local),
    )


def test_attested_tree_sha_picks_newest_real_run_against() -> None:
    from foundry.agents.quality.reviewer_critic import _attested_tree_sha

    # SimpleNamespace.seq needs a real ordering; use ints via a tiny shim.
    class _Seq(int):
        pass

    g1 = SimpleNamespace(name="build", run_against="tree-A", seq=_Seq(1))
    g2 = SimpleNamespace(name="test", run_against="tree-A", seq=_Seq(2))
    state = {"gates": (g1, g2)}
    assert _attested_tree_sha(state) == "tree-A"


def test_attested_tree_sha_none_before_any_gate() -> None:
    from foundry.agents.quality.reviewer_critic import _attested_tree_sha

    assert _attested_tree_sha({"gates": ()}) is None
    assert _attested_tree_sha({}) is None


def test_attested_tree_sha_ignores_empty_run_against() -> None:
    from foundry.agents.quality.reviewer_critic import _attested_tree_sha

    class _Seq(int):
        pass

    g = SimpleNamespace(name="review", run_against="", seq=_Seq(5))
    assert _attested_tree_sha({"gates": (g,)}) is None
