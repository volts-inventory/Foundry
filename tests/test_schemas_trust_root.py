"""Conformance tests for the Phase-4 (Trust root) artifact schemas. (T-M4-01)

Contract under test (docs/governance.md §4–§5; conventions from
foundry/schemas/base.py): every persisted record is a FrozenModel (frozen,
extra="forbid"), carries `seq: Seq` where it is a channel record, has a
content_identity() stable across construction order and sensitive to field
changes. Bodies/predicates are content-addressed `*_uri`/`*_digest` handles
(DS9). The load-bearing properties:

  * the SLSA provenance predicate is INPUT-BOUND — changing ANY material digest
    (or the produced subject) changes its identity (governance.md §4);
  * an AdmissionDecision carries the EXACT digest + the precise gate-state /
    signature grounds it verified, and is fail-closed (gov §5).

The records are real; their cryptographic ROOT is dev-grade (plan §0.3).
The existing security.py / operations.py records are unchanged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.base import FrozenModel
from foundry.schemas.operations import (
    AdmissionDecision,
    AdmissionVerdict,
    BuildRequest,
    BuildResult,
    DeployResult,
)
from foundry.schemas.security import (
    SBOM,
    Material,
    SBOMComponent,
    SLSAProvenance,
)
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="builder", local=0)

_MAT_A = Material(uri="git+https://example/repo", digest="sha256:aaa")
_MAT_B = Material(uri="pkg:pypi/leftpad@1.0", digest="sha256:bbb")
_COMP = SBOMComponent(
    name="leftpad", version="1.0", digest="sha256:bbb", license="MIT"
)


def _provenance(**overrides: object) -> SLSAProvenance:
    kwargs: dict[str, object] = {
        "build_type": "hermetic-v1",
        "builder_id": "dev-grade-builder",
        "invocation_uri": "cas://invocation/1",
        "source_uri": "git+https://example/repo",
        "source_digest": "sha256:src",
        "tree_sha": "treesha-1",
        "subject_name": "app",
        "subject_digest": "sha256:subject",
        "materials": (_MAT_A, _MAT_B),
        "gate_result_uris": ("cas://gate/build", "cas://gate/test"),
        "scanner_config_rev": "scanrev-1",
        "seq": SEQ,
    }
    kwargs.update(overrides)
    return SLSAProvenance(**kwargs)


# (model, valid kwargs, field to change, changed value) — one row per seq'd record.
CASES: list[tuple[type[FrozenModel], dict, str, object]] = [
    (
        SLSAProvenance,
        {
            "build_type": "hermetic-v1",
            "builder_id": "dev-grade-builder",
            "invocation_uri": "cas://invocation/1",
            "source_uri": "git+https://example/repo",
            "source_digest": "sha256:src",
            "tree_sha": "treesha-1",
            "subject_name": "app",
            "subject_digest": "sha256:subject",
            "materials": (_MAT_A, _MAT_B),
            "gate_result_uris": ("cas://gate/build",),
            "scanner_config_rev": "scanrev-1",
            "seq": SEQ,
        },
        "subject_digest",
        "sha256:other",
    ),
    (
        SBOM,
        {
            "tree_sha": "treesha-1",
            "sbom_uri": "cas://sbom/1",
            "components": (_COMP,),
            "seq": SEQ,
        },
        "tree_sha",
        "treesha-2",
    ),
    (
        BuildRequest,
        {
            "build_type": "hermetic-v1",
            "source_uri": "git+https://example/repo",
            "source_digest": "sha256:src",
            "tree_sha": "treesha-1",
            "base_image_digest": "sha256:base",
            "build_config_uri": "cas://invocation/1",
            "material_digests": ("sha256:aaa", "sha256:bbb"),
            "seq": SEQ,
        },
        "base_image_digest",
        "sha256:tampered",
    ),
    (
        BuildResult,
        {
            "subject_digest": "sha256:subject",
            "tree_sha": "treesha-1",
            "source_digest": "sha256:src",
            "builder_id": "dev-grade-builder",
            "provenance_uri": "cas://prov/1",
            "sbom_uri": "cas://sbom/1",
            "signature": "dev-sig",
            "seq": SEQ,
        },
        "subject_digest",
        "sha256:other",
    ),
    (
        AdmissionDecision,
        {
            "subject_digest": "sha256:subject",
            "tree_sha": "treesha-1",
            "verdict": AdmissionVerdict.ADMIT,
            "reason": "ok",
            "signature_verified": True,
            "all_required_gates_pass": True,
            "required_gates": ("build", "test", "security"),
            "gate_state_uri": "cas://gatestate/1",
            "scanner_config_rev": "scanrev-1",
            "seq": SEQ,
        },
        "verdict",
        AdmissionVerdict.DENY,
    ),
    (
        DeployResult,
        {
            "deploy_id": "deploy-1",
            "subject_digest": "sha256:subject",
            "idempotency_key": "idem-1",
            "admission_decision_uri": "cas://admission/1",
            "rollback_to_digest": "sha256:prior",
            "deploy_plan_uri": "cas://deployplan/1",
            "seq": SEQ,
        },
        "idempotency_key",
        "idem-2",
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_constructs_and_is_frozen(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_extra_fields_rejected(cls, kwargs, field, changed):
    with pytest.raises(ValidationError):
        cls(**kwargs, not_a_declared_field="boom")


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_content_identity_stable_and_field_sensitive(cls, kwargs, field, changed):
    a = cls(**kwargs)
    b = cls(**dict(reversed(list(kwargs.items()))))  # construction order irrelevant
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic
    mutated = a.model_copy(update={field: changed})
    assert mutated.content_identity() != a.content_identity()


# ---------------------------------------------------------------------------
# Non-seq sub-records (Material / SBOMComponent) are frozen + extra-forbid too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("inst", [_MAT_A, _COMP])
def test_subrecords_frozen_and_extra_forbid(inst):
    cls = type(inst)
    with pytest.raises(ValidationError):
        cls(**inst.model_dump(), not_a_field="boom")
    field = next(iter(cls.model_fields))
    with pytest.raises(ValidationError):
        setattr(inst, field, "boom")


def test_subrecords_content_identity_stable():
    assert _MAT_A.content_identity() == Material(
        uri="git+https://example/repo", digest="sha256:aaa"
    ).content_identity()
    assert _MAT_A.content_identity() != _MAT_B.content_identity()


# ---------------------------------------------------------------------------
# Load-bearing invariant: the provenance predicate is INPUT-BOUND. (gov §4)
# ---------------------------------------------------------------------------


def test_provenance_binds_its_materials_changing_a_material_changes_identity():
    """Changing ANY material's digest changes the predicate identity — the
    signature attests over the pinned input set (governance.md §4)."""
    base = _provenance()
    # Same materials, declared in the SAME order, with one digest flipped.
    tampered_mat = _MAT_B.model_copy(update={"digest": "sha256:evil"})
    tampered = base.model_copy(update={"materials": (_MAT_A, tampered_mat)})
    assert tampered.content_identity() != base.content_identity()
    assert base.material_digests() == ("sha256:aaa", "sha256:bbb")
    assert tampered.material_digests() == ("sha256:aaa", "sha256:evil")


def test_provenance_binds_its_produced_subject_digest():
    """The predicate binds the produced subject — the exact digest admission
    re-verifies (governance.md §5)."""
    base = _provenance()
    assert base.binds_subject("sha256:subject") is True
    assert base.binds_subject("sha256:other") is False
    rebuilt = base.model_copy(update={"subject_digest": "sha256:other"})
    assert rebuilt.content_identity() != base.content_identity()


def test_provenance_material_order_is_part_of_binding():
    """Reordering materials yields a different bound input set (tuple order is
    part of identity)."""
    base = _provenance()
    reordered = base.model_copy(update={"materials": (_MAT_B, _MAT_A)})
    assert reordered.content_identity() != base.content_identity()


def test_provenance_binds_gate_state_and_scanner_config():
    """The predicate binds the trusted-runner gate-state + the pinned
    scanner-config rev (governance.md §1c/§4) — both ride content_identity."""
    base = _provenance()
    swapped_gates = base.model_copy(
        update={"gate_result_uris": ("cas://gate/build",)}
    )
    assert swapped_gates.content_identity() != base.content_identity()
    swapped_cfg = base.model_copy(update={"scanner_config_rev": "scanrev-2"})
    assert swapped_cfg.content_identity() != base.content_identity()


# ---------------------------------------------------------------------------
# Load-bearing invariant: AdmissionDecision carries the EXACT digest + the
# verified gate-state, and is fail-closed. (gov §5)
# ---------------------------------------------------------------------------


def test_admission_admit_carries_exact_digest_and_verified_gate_state():
    d = AdmissionDecision(
        subject_digest="sha256:subject",
        tree_sha="treesha-1",
        verdict=AdmissionVerdict.ADMIT,
        reason="ok",
        signature_verified=True,
        all_required_gates_pass=True,
        required_gates=("build", "test", "security"),
        gate_state_uri="cas://gatestate/1",
        scanner_config_rev="scanrev-1",
        seq=SEQ,
    )
    assert d.admitted() is True
    assert d.subject_digest == "sha256:subject"  # the EXACT digest
    assert d.signature_verified is True  # which signature it verified
    assert d.all_required_gates_pass is True  # the gate-state it verified
    assert "build" in d.required_gates


def test_admission_is_fail_closed_on_non_admit_verdicts():
    """DENY and INDETERMINATE are NOT admission — a missing/indeterminate ground
    never silently ships (gov §5)."""
    base = AdmissionDecision(
        subject_digest="sha256:subject",
        tree_sha="treesha-1",
        verdict=AdmissionVerdict.DENY,
        reason="stable_fail_latched",
        stable_fail_latched=True,
        seq=SEQ,
    )
    assert base.admitted() is False
    indeterminate = base.model_copy(
        update={"verdict": AdmissionVerdict.INDETERMINATE, "reason": "revocation_unreachable"}
    )
    assert indeterminate.admitted() is False


def test_admission_default_is_fail_closed():
    """An AdmissionDecision constructed with no verified grounds defaults to NOT
    admitted (the grounds default False)."""
    d = AdmissionDecision(
        subject_digest="sha256:subject",
        tree_sha="treesha-1",
        verdict=AdmissionVerdict.DENY,
        reason="sig_invalid",
        seq=SEQ,
    )
    assert d.signature_verified is False
    assert d.all_required_gates_pass is False
    assert d.stable_fail_latched is False
    assert d.admitted() is False


def test_admission_verdict_taxonomy():
    assert {v.value for v in AdmissionVerdict} == {"admit", "deny", "indeterminate"}


# ---------------------------------------------------------------------------
# Build + SBOM spot checks.
# ---------------------------------------------------------------------------


def test_build_result_binds_inputs_to_subject_digest():
    r = BuildResult(
        subject_digest="sha256:subject",
        tree_sha="treesha-1",
        source_digest="sha256:src",
        builder_id="dev-grade-builder",
        provenance_uri="cas://prov/1",
        sbom_uri="cas://sbom/1",
        signature="dev-sig",
        seq=SEQ,
    )
    # Re-binding the produced digest re-keys identity (input-bound output).
    other = r.model_copy(update={"subject_digest": "sha256:other"})
    assert other.content_identity() != r.content_identity()
    assert r.hermetic is True  # dev-grade hermetic posture default


def test_build_request_pins_inputs_and_defaults_hermetic():
    req = BuildRequest(
        build_type="hermetic-v1",
        source_uri="git+https://example/repo",
        source_digest="sha256:src",
        tree_sha="treesha-1",
        base_image_digest="sha256:base",
        build_config_uri="cas://invocation/1",
        seq=SEQ,
    )
    assert req.material_digests == ()  # immutable tuple default (S1-safe)
    assert req.hermetic is True


def test_sbom_full_resolution_and_unpinned_detection():
    """The Audit gate fails on any unpinned component (gov §4)."""
    good = SBOM(tree_sha="t", sbom_uri="cas://sbom/1", components=(_COMP,), seq=SEQ)
    assert good.fully_resolved is True
    assert good.unpinned_components() == ()
    unpinned = SBOMComponent(name="ghost", version="0.0", digest="")
    bad = good.model_copy(update={"components": (_COMP, unpinned)})
    assert bad.unpinned_components() == ("ghost",)


def test_sbom_is_fuller_than_the_sbomref_handle():
    """SBOM carries the resolved inventory inline; SBOMRef is just a handle."""
    sbom = SBOM(tree_sha="t", sbom_uri="cas://sbom/1", components=(_COMP,), seq=SEQ)
    assert sbom.components[0].name == "leftpad"
    assert sbom.components[0].digest == "sha256:bbb"
    assert sbom.components[0].license == "MIT"


def test_deploy_result_idempotent_and_rollback_capable():
    """DeployResult binds the deployed digest to a process-stable idempotency
    key and carries a rollback pointer (gov §5)."""
    d = DeployResult(
        deploy_id="deploy-1",
        subject_digest="sha256:subject",
        idempotency_key="idem-1",
        admission_decision_uri="cas://admission/1",
        rollback_to_digest="sha256:prior",
        seq=SEQ,
    )
    assert d.rolled_back is False
    assert d.rollback_to_digest == "sha256:prior"
    rolled = d.model_copy(update={"rolled_back": True})
    assert rolled.content_identity() != d.content_identity()
