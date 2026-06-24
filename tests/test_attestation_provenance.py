"""Input-bound attestation + provenance binding (T-M4-05).

The forged-verdict / replay defense (governance.md §4/§5, Phase-4 adversarial):
a deterministic-gate attestation binds the verdict to its EXACT inputs (gate +
tree/state_sha + scanner-config + lineage + status + results), so the SAME
attestation cannot verify against a DIFFERENT tree, config, or a flipped status;
and a BUILD attestation additionally binds the SLSA provenance predicate's
identity (builder + materials + subject digest), so it cannot be replayed onto a
different build/subject. Exercised directly against the dev-grade HMAC verifier
in foundry/gates/attestation.py (plan §0.3 — real logic, dev-grade root).
"""

from __future__ import annotations

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.security import Material, SLSAProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq

KEY = b"zone-b-attestation-provenance-key"
KEY_ID = "dev-key-prov"


def make_seq(node_id: str = "build_runner", *, step: int = 1) -> Seq:
    return Seq(
        run_id="run-1",
        emission_epoch=0,
        superstep_index=step,
        node_id=node_id,
        local=0,
    )


def make_det_gate(
    *,
    name: str = "build",
    status: GateStatus = GateStatus.PASS,
    run_against: str = "treeA",
    scanner_config_rev: str = "configX",
    seq: Seq | None = None,
    **over: object,
) -> GateResult:
    fields: dict[str, object] = dict(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        scanner_config_rev=scanner_config_rev,
        lineage_map_revision="lineage-1",
        results_digest="resdigest-1",
        run_against=run_against,
        seq=seq or make_seq(),
    )
    fields.update(over)
    return GateResult(**fields)


def make_provenance(
    *,
    subject_digest: str = "sha256:" + "a" * 64,
    builder_id: str = "dev-builder-1",
    tree_sha: str = "treeA",
    materials: tuple[Material, ...] = (
        Material(uri="git://repo", digest="sha256:" + "b" * 64),
    ),
    **over: object,
) -> SLSAProvenance:
    fields: dict[str, object] = dict(
        build_type="dev-build",
        builder_id=builder_id,
        invocation_uri="cas://invocation/1",
        source_uri="git://repo",
        source_digest="sha256:" + "c" * 64,
        tree_sha=tree_sha,
        subject_name="app",
        subject_digest=subject_digest,
        materials=materials,
        seq=make_seq("provenance"),
    )
    fields.update(over)
    return SLSAProvenance(**fields)


@pytest.fixture
def signer() -> ZoneBSigner:
    return ZoneBSigner(KEY, KEY_ID)


@pytest.fixture
def verifier() -> DevGateWriteVerifier:
    return DevGateWriteVerifier(KEY, KEY_ID, GlobalRecords())


# ---- 1. the attestation is fully input-bound: cross-tree replay blocked --------


def test_attestation_verifies_against_its_own_inputs(signer, verifier):
    g = signer.with_attestation(make_det_gate())
    assert verifier.verify_attestation(g) is True


def test_cross_tree_replay_blocked(signer, verifier):
    # Signed for tree A; re-presented against tree B (only run_against changed).
    signed = signer.with_attestation(make_det_gate(run_against="treeA"))
    replayed = signed.model_copy(update={"run_against": "treeB"})
    assert verifier.verify_attestation(replayed) is False


def test_cross_config_replay_blocked(signer, verifier):
    # Signed for config X; re-presented under config Y.
    signed = signer.with_attestation(make_det_gate(scanner_config_rev="configX"))
    replayed = signed.model_copy(update={"scanner_config_rev": "configY"})
    assert verifier.verify_attestation(replayed) is False


def test_flipped_status_replay_blocked(signer, verifier):
    # Signed PASS; the adversary flips the verdict bit to FAIL.
    signed = signer.with_attestation(make_det_gate(status=GateStatus.PASS))
    flipped = signed.model_copy(update={"status": GateStatus.FAIL})
    assert verifier.verify_attestation(flipped) is False


def test_lineage_and_results_digest_bound(signer, verifier):
    signed = signer.with_attestation(make_det_gate())
    assert verifier.verify_attestation(
        signed.model_copy(update={"lineage_map_revision": "lineage-2"})
    ) is False
    assert verifier.verify_attestation(
        signed.model_copy(update={"results_digest": "tampered"})
    ) is False


# ---- 2. provenance binding for build artifacts ---------------------------------


def test_provenance_binding_verifies_for_its_provenance(signer, verifier):
    prov = make_provenance()
    g = signer.with_provenance_binding(make_det_gate(), prov)
    assert verifier.verify_provenance_binding(g, prov) is True


def test_tampered_subject_digest_fails(signer, verifier):
    prov = make_provenance(subject_digest="sha256:" + "a" * 64)
    g = signer.with_provenance_binding(make_det_gate(), prov)
    # A different produced-subject digest -> different content_identity().
    tampered = make_provenance(subject_digest="sha256:" + "f" * 64)
    assert tampered.content_identity() != prov.content_identity()
    assert verifier.verify_provenance_binding(g, tampered) is False


def test_tampered_material_digest_fails(signer, verifier):
    prov = make_provenance()
    g = signer.with_provenance_binding(make_det_gate(), prov)
    swapped = make_provenance(
        materials=(Material(uri="git://repo", digest="sha256:" + "9" * 64),)
    )
    assert verifier.verify_provenance_binding(g, swapped) is False


def test_tampered_builder_id_fails(signer, verifier):
    prov = make_provenance(builder_id="dev-builder-1")
    g = signer.with_provenance_binding(make_det_gate(), prov)
    other_builder = make_provenance(builder_id="rogue-builder")
    assert verifier.verify_provenance_binding(g, other_builder) is False


def test_build_attestation_not_replayable_onto_different_tree(signer, verifier):
    # A provenance-bound build verdict still carries the input-bound base tuple:
    # flipping the gate's run_against (tree) breaks verification even with the
    # original provenance re-presented.
    prov = make_provenance(tree_sha="treeA")
    g = signer.with_provenance_binding(make_det_gate(run_against="treeA"), prov)
    replayed = g.model_copy(update={"run_against": "treeB"})
    assert verifier.verify_provenance_binding(replayed, prov) is False


def test_plain_attestation_check_rejects_provenance_bound_signature(signer, verifier):
    # A provenance-bound signature is NOT a valid plain attestation (it folds in
    # the provenance identity), so a build verdict can't masquerade as a generic
    # deterministic verdict to dodge the provenance re-check.
    prov = make_provenance()
    g = signer.with_provenance_binding(make_det_gate(), prov)
    assert verifier.verify_attestation(g) is False


def test_missing_attestation_fails(signer, verifier):
    prov = make_provenance()
    bare = make_det_gate()  # never signed
    assert verifier.verify_provenance_binding(bare, prov) is False


# ---- 3. memoization is sound (per (verdict, provenance) pair) -------------------


def test_provenance_verification_memoized_per_pair(signer, verifier):
    prov = make_provenance()
    g = signer.with_provenance_binding(make_det_gate(), prov)
    assert verifier.verify_provenance_binding(g, prov) is True
    # Cached true is returned on the repeat; a different provenance is a distinct
    # cache key and still fails (no stale-true leak across provenances).
    assert verifier.verify_provenance_binding(g, prov) is True
    other = make_provenance(subject_digest="sha256:" + "e" * 64)
    assert verifier.verify_provenance_binding(g, other) is False
