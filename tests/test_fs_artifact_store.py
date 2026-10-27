"""ArtifactStore + FilesystemArtifactStore (TASK [14]) — one content-addressed contract.

The in-memory ArtifactStore is dev-grade; the filesystem store is its production
drop-in for the docker-compose / EKS profile (state behind the SAME interface so
AgentRuntime.cas swaps unchanged). The contract tests run against BOTH stores
(the drop-in guarantee). These tests pin:

* put_body / get_body round-trip several schema types; the uri ==
  cas://sha256/<content_identity> (IDENTICAL to what emit_artifact mints and to
  the in-memory store); put_body_at stores under an explicit handle uri.
* put_blob / get_blob round-trip raw bytes/str, keyed by the RAW sha256 (the
  sandbox-compatible digest) so a mirrored diff keeps its uri.
* durability: a fresh filesystem store re-opened on the same root reads back
  what an earlier one wrote (the property the in-memory store lacks).
* fail-closed: unknown uri get_body/get_blob raise UnknownArtifactError.
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


@pytest.fixture(params=["memory", "filesystem"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        return ArtifactStore()
    return FilesystemArtifactStore(tmp_path)


# ---- put_body / get_body round-trip (several schema types) -------------------


@pytest.mark.parametrize("body", [_criterion(), _system_design(), _feature_diff()])
def test_put_body_get_body_round_trips_typed_bodies(store: Any, body: Any) -> None:
    uri = store.put_body(body)
    # uri is the body's content identity (== what emit_artifact mints), so both
    # backends land a body at the SAME uri.
    assert uri == f"cas://sha256/{body.content_identity()}"
    back = store.get_body(uri)
    assert back == body
    assert type(back) is type(body)


def test_put_body_at_explicit_uri_for_junior_feature_diff(store: Any) -> None:
    """The junior's FeatureDiff handle uri carries the idempotency KEY, not the
    body content identity; put_body_at stores it under that exact uri (whose
    slashes must NOT become path separators on disk)."""
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


@pytest.mark.parametrize("getter", ["get_body", "get_blob"])
def test_unknown_uri_raises_fail_closed(store: Any, getter: str) -> None:
    with pytest.raises(UnknownArtifactError):
        getattr(store, getter)("cas://sha256/does-not-exist")


# ---- put_blob / get_blob (raw bytes, sandbox-compatible digest) --------------


def test_put_blob_get_blob_round_trips_bytes_and_str(store: Any) -> None:
    body = b"--- /dev/null\n+++ b/x.py\n@@\n+print(1)\n"
    uri = store.put_blob(body)
    assert uri.startswith("cas://sha256/")
    assert store.get_blob(uri) == body
    # str is encoded utf-8 and keyed by the SAME raw sha256 as the bytes.
    assert store.put_blob("hello") == store.put_blob(b"hello")
    assert store.get_blob(store.put_blob("hello")) == b"hello"


def test_put_blob_matches_sandbox_raw_sha256(store: Any) -> None:
    """The store keys blobs by the RAW sha256 of the bytes — EXACTLY
    Sandbox.put_blob — so a diff blob mirrored sandbox->store keeps its uri."""
    data = b"some diff body"
    uri = store.put_blob(data)
    assert uri == f"cas://sha256/{hashlib.sha256(data).hexdigest()}"


# ---- DURABILITY (the property the in-mem store lacks) ------------------------


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
