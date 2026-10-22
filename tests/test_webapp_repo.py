"""The Workshop reads a finished build's repository straight from the durable store.

``webapp.repo.read_repo`` reconstructs the integrated tree from the durable CAS +
checkpoint — NO output directory. This is the source of truth behind the
self-hosted "GitHub" tab: the code, the gate verdicts (as checks), and each
FeatureDiff (as a 'pull request') all come back out of the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from foundry.agents._common import emit_artifact, persist_prose
from foundry.cli import run
from foundry.durability.artifact_store import ArtifactStore
from foundry.schemas.architecture import (
    ConstraintScope,
    InterfaceContract,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.product import PRD
from foundry.state.seq import Seq
from webapp.repo import RepoUnavailable, plan_context, read_repo


def test_read_repo_materializes_tree_from_the_store(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    # A real (mock) build, durably checkpointed — but written to NO output dir.
    result = run(
        "build a tiny URL-shortener service",
        thread_id="repo-1",
        state_dir=state_dir,
    )
    assert result.passed is True
    assert result.output_root is None  # nothing on disk — it's all in the store

    repo = read_repo("repo-1", state_dir)
    # The integrated tree comes back from the CAS (the stub build's one file),
    # and the Spec gate's criteria.json INPUT is excluded (it's not a source file).
    paths = {f.path for f in repo.files}
    assert paths and "criteria.json" not in paths
    # Every gate verdict is surfaced as a status check, all green for a passing build.
    checks = {c.name: c for c in repo.checks}
    assert checks and all(c.ok for c in checks.values())
    assert "build" in checks and "test" in checks
    # The FeatureDiff is surfaced as a 'pull request' with its unified-diff body.
    assert repo.diffs and all(d.text for d in repo.diffs)
    # file_bytes returns the exact materialized content.
    first = next(iter(paths))
    assert repo.file_bytes(first) is not None
    assert repo.tree_sha  # the integrated tree_sha the gates ran against


def _parent_state() -> tuple[dict[str, Any], ArtifactStore]:
    cas = ArtifactStore()

    def s(local: int) -> Seq:
        return Seq("parent", 1, local, "planner", 0)

    def emit(model: Any, kind: str) -> Any:
        return emit_artifact(model, kind, "planner", model.seq, store=cas)

    def prose(text: str) -> str:
        return persist_prose(text, store=cas)

    pc = PinnedConstraint(id="PC-1", text="Never log tokens.", scope=ConstraintScope.GLOBAL,
                          source="ADR-1", seq=s(1))
    gone = PinnedConstraint(id="PC-2", text="Use tabs.", scope=ConstraintScope.LOCAL,
                            source="ADR-1", seq=s(1))
    state = {
        "artifacts": [
            emit(PRD(title="Old notes", prd_uri=prose("old body"), seq=s(1)), "prd"),
            emit(PRD(title="Notes", prd_uri=prose("Users keep notes."), seq=s(2)), "prd"),
            emit(SystemDesign(title="Notes design", design_uri=prose("web -> api -> store"),
                              components=("api", "store"), interface_ids=("Store.load",),
                              seq=s(2)), "system_design"),
            emit(InterfaceContract(name="Store.load", spec_uri=prose("load(id) -> note"),
                                   seq=s(2)), "interface_contract"),
            emit(InterfaceContract(name="Stale.x", spec_uri=prose("gone()"), seq=s(1)),
                 "interface_contract"),
        ],
        "pinned_constraints": [
            pc, gone, gone.model_copy(update={"supersedes": "PC-2", "seq": s(3)}),
        ],
    }
    return state, cas


def test_plan_context_carries_the_parents_shipped_plan() -> None:
    state, cas = _parent_state()
    text = plan_context(state, cas)
    assert "PRD: Notes\nUsers keep notes." in text and "Old notes" not in text
    assert "System design: Notes design\nweb -> api -> store" in text
    assert "- Store.load: load(id) -> note" in text and "Stale.x" not in text
    assert "- Never log tokens." in text and "Use tabs." not in text
    assert len(plan_context(state, cas, cap=50)) == 50
    assert plan_context({}, cas) == ""


def test_an_iterate_runs_with_the_parents_plan_as_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from webapp import server

    state, cas = _parent_state()
    monkeypatch.setattr(server, "read_plan_context",
                        lambda pid, _dir: plan_context(state, cas) if pid == "par" else "")
    child = server.Project(id="c", request="add tags", mode="assistant", floor=1,
                           parent_id="par", spec="tags on notes")
    spec = server._child_spec(child)
    assert spec is not None
    assert spec.startswith("tags on notes\n\n") and "PRD: Notes" in spec
    fresh = server.Project(id="f", request="x", mode="assistant", floor=2, spec="s")
    assert server._child_spec(fresh) == "s"


def test_read_repo_unavailable_without_a_checkpoint(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    run("build a tiny URL-shortener service", thread_id="repo-2", state_dir=state_dir)
    with pytest.raises(RepoUnavailable):
        read_repo("no-such-build", state_dir)
