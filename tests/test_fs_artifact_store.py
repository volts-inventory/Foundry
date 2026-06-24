"""FilesystemArtifactStore (TASK [14]) — durable, content-addressed drop-in.

The in-memory ArtifactStore is dev-grade; the filesystem store is its production
drop-in for the docker-compose / EKS profile (state behind the SAME interface so
AgentRuntime.cas swaps unchanged). These tests pin:

* put_body / get_body round-trip several schema types; the uri ==
  cas://sha256/<content_identity> (IDENTICAL to what emit_artifact mints and to
  the in-memory store); put_body_at stores under an explicit handle uri.
* put_blob / get_blob round-trip raw bytes/str, keyed by the RAW sha256 (the
  sandbox-compatible digest) so a mirrored diff keeps its uri.
* content-address STABILITY: the same body/blob yields the same uri across
  store instances and survives a fresh store re-opened on the same root (the
  durability property the in-memory store lacks).
* fail-closed: unknown uri get_body/get_blob raise UnknownArtifactError.
* interface parity with the in-memory ArtifactStore (drop-in guarantee).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from foundry.durability.artifact_store import ArtifactStore, UnknownArtifactError
from foundry.durability.fs_artifact_store import FilesystemArtifactStore
from foundry.schemas.architecture import SystemDesign
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.seq import Seq


def _seq(node_id: str = "n", local: int = 0) -> Seq:
    return Seq(
        run_id="run-fs-store",
        emission_epoch=0,
        superstep_index=0,
        node_id=node_id,
        local=local,
    )


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id="AC-001",
        text="Given a user, When they add a todo, Then it appears.",
        criticality=Criticality.HIGH_RISK,
        oracle_hint="POST then GET returns the item",
        security_abuse=True,
        seq=_seq("product_manager"),
    )


def _system_design() -> SystemDesign:
    return SystemDesign(
        title="Todo service",
        design_uri="cas://sha256/deadbeef",
        components=("api", "store"),
        adr_ids=("ADR-1",),
        seq=_seq("principal_architect"),
    )


def _feature_diff() -> FeatureDiff:
    return FeatureDiff(
        diff_uri="cas://sha256/cafef00d",
        task_id="T1",
        branch="task/T1",
        base_tree_sha="base-sha",
        seq=_seq("junior_engineer"),
    )


# ---- put_body / get_body round-trip (several schema types) -------------------


@pytest.mark.parametrize("body", [_criterion(), _system_design(), _feature_diff()])
def test_put_body_get_body_round_trips_typed_bodies(tmp_path: Path, body: Any) -> None:
    store = FilesystemArtifactStore(tmp_path)
    uri = store.put_body(body)
    # uri is the body's content identity (== what emit_artifact / the in-mem store mint).
    assert uri == f"cas://sha256/{body.content_identity()}"
    back = store.get_body(uri)
    assert back == body
    assert type(back) is type(body)


def test_put_body_at_explicit_uri_for_junior_feature_diff(tmp_path: Path) -> None:
    """The junior's FeatureDiff handle uri carries the idempotency KEY, not the
    body content identity; put_body_at stores it under that exact uri (whose
    slashes must NOT become path separators on disk)."""
    store = FilesystemArtifactStore(tmp_path)
    diff = _feature_diff()
    handle_uri = "cas://feature_diff/some-idempotency-key"
    assert store.put_body_at(handle_uri, diff) == handle_uri
    assert store.get_body(handle_uri) == diff


def test_resolve_view_reconstitutes_handle_body(tmp_path: Path) -> None:
    """resolve() is the ArtifactResolver seam — get_body keyed on artifact.uri."""
    store = FilesystemArtifactStore(tmp_path)
    crit = _criterion()
    uri = store.put_body(crit)

    class _Handle:  # minimal object exposing .uri (resolve reads only that)
        def __init__(self, u: str) -> None:
            self.uri = u

    assert store.resolve(_Handle(uri)) == crit
    assert store.resolve_blob(store.put_blob(b"raw")) == b"raw"


def test_get_body_unknown_uri_raises_fail_closed(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(UnknownArtifactError):
        store.get_body("cas://sha256/does-not-exist")


def test_get_blob_unknown_uri_raises_fail_closed(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(UnknownArtifactError):
        store.get_blob("cas://sha256/does-not-exist")


# ---- put_blob / get_blob (raw bytes, sandbox-compatible digest) --------------


def test_put_blob_get_blob_round_trips_bytes_and_str(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    body = b"--- /dev/null\n+++ b/x.py\n@@\n+print(1)\n"
    uri = store.put_blob(body)
    assert uri.startswith("cas://sha256/")
    assert store.get_blob(uri) == body
    # str is encoded utf-8 and keyed by the SAME raw sha256 as the bytes.
    assert store.put_blob("hello") == store.put_blob(b"hello")
    assert store.get_blob(store.put_blob("hello")) == b"hello"


def test_put_blob_matches_sandbox_raw_sha256(tmp_path: Path) -> None:
    """The store keys blobs by the RAW sha256 of the bytes — EXACTLY
    Sandbox.put_blob / the in-memory store — so a diff blob mirrored
    sandbox->store keeps its uri."""
    store = FilesystemArtifactStore(tmp_path)
    data = b"some diff body"
    uri = store.put_blob(data)
    assert uri == f"cas://sha256/{hashlib.sha256(data).hexdigest()}"


# ---- content-address STABILITY + DURABILITY (the property the in-mem lacks) --


def test_content_address_stable_across_instances(tmp_path: Path) -> None:
    """The uri is a pure function of content — two independent stores (even on
    different roots) mint the SAME uri for the same body/blob."""
    a = FilesystemArtifactStore(tmp_path / "a")
    b = FilesystemArtifactStore(tmp_path / "b")
    crit = _criterion()
    assert a.put_body(crit) == b.put_body(crit)
    assert a.put_blob(b"x") == b.put_blob(b"x")


def test_body_and_blob_survive_store_reopen_on_same_root(tmp_path: Path) -> None:
    """Durability: a store re-opened on the same root reads back what an earlier
    store wrote — the production property the in-memory dict store cannot offer."""
    writer = FilesystemArtifactStore(tmp_path)
    crit = _criterion()
    body_uri = writer.put_body(crit)
    blob_uri = writer.put_blob(b"unified diff bytes")
    del writer

    reader = FilesystemArtifactStore(tmp_path)
    assert reader.get_body(body_uri) == crit
    assert reader.get_blob(blob_uri) == b"unified diff bytes"


def test_put_body_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    crit = _criterion()
    assert store.put_body(crit) == store.put_body(crit)
    assert store.get_body(store.put_body(crit)) == crit


# ---- drop-in interface parity with the in-memory ArtifactStore ---------------


def test_interface_parity_with_in_memory_store() -> None:
    """Every public seam AgentRuntime.cas uses must exist with the same name on
    the filesystem store — the drop-in guarantee."""
    seams = [
        "put_body",
        "put_body_at",
        "get_body",
        "put_blob",
        "get_blob",
        "resolve",
        "resolve_blob",
    ]
    for name in seams:
        assert hasattr(FilesystemArtifactStore, name), name
        assert hasattr(ArtifactStore, name), name


def test_filesystem_and_in_memory_mint_identical_uris(tmp_path: Path) -> None:
    """A body/blob put through either backend lands at the SAME cas uri — so a
    handle minted under one resolves under the other."""
    mem = ArtifactStore()
    fs = FilesystemArtifactStore(tmp_path)
    crit = _criterion()
    assert mem.put_body(crit) == fs.put_body(crit)
    assert mem.put_blob(b"diff") == fs.put_blob(b"diff")
