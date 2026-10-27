"""Single-host durable persistence — survive a PROCESS RESTART (durability/).

The dev-grade stores are in-memory; ``FOUNDRY_STATE_DIR`` makes one host durable
via the interfaces that already accept a path / connection
(:mod:`foundry.durability.persistence`). These tests pin the restart-survival
property and the byte-identical default:

* a stable-FAIL latch + a LangGraph checkpoint + an artifact body written via
  file-backed instances are STILL there after those instances are DROPPED and
  NEW ones are re-opened on the SAME state dir (simulating a process restart);
* ``FOUNDRY_STATE_DIR`` UNSET ⇒ :memory: + in-memory dicts (no file written),
  byte-identical to today.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.persistence import (
    durable_backends,
    state_dir_from_env,
)
from foundry.schemas.architecture import SystemDesign
from foundry.state.global_records import AdmissionState, GlobalRecords
from foundry.state.seq import Seq

_CONFIG = {"configurable": {"thread_id": "restart-thread", "checkpoint_ns": ""}}
_METADATA = {"source": "loop", "step": 1, "parents": {}}

_DIGEST = "digest-stable-fail"
_GATE = "build"


def _seq() -> Seq:
    return Seq(
        run_id="run-durable",
        emission_epoch=0,
        superstep_index=0,
        node_id="principal_architect",
        local=0,
    )


def _system_design() -> SystemDesign:
    return SystemDesign(
        title="Durable service",
        design_uri="cas://sha256/deadbeef",
        components=("api",),
        seq=_seq(),
    )


def test_state_survives_process_restart(tmp_path: Path) -> None:
    """Write via file-backed instances, DROP them, re-open on the same dir.

    (a) a stable-FAIL latch + a checkpoint + an artifact body, (b) drop the
    instances, (c) open NEW ones on the SAME dir and assert the latch is still
    latched, the checkpoint thread resumes, and the artifact body resolves.
    """
    state_dir = tmp_path / "state"

    # -- (a) write via the first set of file-backed instances ----------------
    first = durable_backends(state_dir)
    try:
        first.global_records.latch_stable_fail(_DIGEST, _GATE, "run-durable")
        assert first.global_records.stable_fail_latched(_DIGEST, _GATE) is True

        checkpoint = empty_checkpoint()
        with first.checkpointer.superstep_txn():
            first.checkpointer.saver.put(_CONFIG, checkpoint, _METADATA, {})

        body_uri = first.cas.put_body(_system_design())
        blob_uri = first.cas.put_blob(b"unified-diff-body")
    finally:
        # -- (b) DROP the instances (release the file handles) ----------------
        first.close()
    del first

    # -- (c) re-open NEW instances pointing at the SAME dir (restart) --------
    second = durable_backends(state_dir)
    try:
        # The trust latch is STILL latched (consumed-nonce / stable-FAIL /
        # alpha-look ledgers survive — this is the property the in-memory store
        # lacks). Admission folds it: the digest stays DISQUALIFIED.
        assert second.global_records.stable_fail_latched(_DIGEST, _GATE) is True
        assert (
            second.global_records.admission_look(_DIGEST)
            is AdmissionState.DISQUALIFIED
        )

        # The checkpoint thread RESUMES (the saver reads the persisted row).
        saved = second.checkpointer.saver.get_tuple(_CONFIG)
        assert saved is not None
        assert saved.checkpoint["id"] == checkpoint["id"]

        # The artifact body + raw blob resolve end to end.
        resolved = second.cas.get_body(body_uri)
        assert isinstance(resolved, SystemDesign)
        assert resolved.title == "Durable service"
        assert second.cas.get_blob(blob_uri) == b"unified-diff-body"
    finally:
        second.close()


def test_state_dir_unset_is_in_memory_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOUNDRY_STATE_DIR UNSET (or blank) ⇒ :memory: + in-memory dicts, no file.

    The default path is byte-identical to today: the seam returns None so the
    caller constructs the in-memory FoundryCheckpointer / GlobalRecords /
    ArtifactStore and writes NOTHING to disk.
    """
    monkeypatch.delenv("FOUNDRY_STATE_DIR", raising=False)
    assert state_dir_from_env() is None

    # A blank value is also treated as unset (never roots state at cwd).
    monkeypatch.setenv("FOUNDRY_STATE_DIR", "   ")
    assert state_dir_from_env() is None

    # The default in-memory backends are unchanged dev-grade types and create
    # no files (mirrors what the cli builds when the env is unset).
    cp = FoundryCheckpointer()
    try:
        assert cp.conn.execute("PRAGMA database_list").fetchone()[2] == ""
    finally:
        cp.close()
    records = GlobalRecords()
    assert records.stable_fail_latched(_DIGEST, _GATE) is False
    store = ArtifactStore()
    body_uri = store.put_body(_system_design())
    assert isinstance(store.get_body(body_uri), SystemDesign)
    # No files were created under tmp_path by the in-memory path.
    assert not any(tmp_path.iterdir())
