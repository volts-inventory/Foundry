"""Hermetic SLSA-L3 trusted builder (T-M4-06).

Exercises the trust-root builder against the dev-grade HMAC root (plan §0.3):

- HERMETIC / REPRODUCIBLE: same pinned inputs ⇒ same subject digest + same
  provenance identity; a non-pinned / un-digested input is REFUSED fail-closed
  (the builder takes only digest-addressed inputs — there is no egress seam, so
  "no network" is structural, not a runtime guard).
- PROVENANCE BOUND: the produced provenance binds materials + subject; flipping a
  material digest re-keys the subject + provenance; the signed attestation
  verifies for the genuine provenance and FAILS for a tampered one (reusing the
  attestation provenance-binding verifier).
- SBOM lists the resolved components; BuildResult carries the signed attestation
  + subject digest.
"""

from __future__ import annotations

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.schemas.security import SBOMComponent
from foundry.schemas.trust_root import BuildRequest
from foundry.state.global_records import GlobalRecords
from foundry.trust_root import (
    BuildOutput,
    NonHermeticBuildError,
    TrustedBuilder,
    is_pinned,
)

KEY = b"trusted-builder-zone-b-key"
KEY_ID = "dev-builder-key"
BUILDER_ID = "dev-trusted-builder-1"

_SRC = "sha256:" + "c" * 64
_BASE = "sha256:" + "d" * 64
_MAT_A = "sha256:" + "a" * 64
_MAT_B = "sha256:" + "b" * 64


def make_request(
    *,
    source_digest: str = _SRC,
    base_image_digest: str = _BASE,
    material_digests: tuple[str, ...] = (_MAT_A, _MAT_B),
    tree_sha: str = "tree-abc",
    hermetic: bool = True,
) -> BuildRequest:
    return BuildRequest(
        build_type="dev-build",
        source_uri="git://repo",
        source_digest=source_digest,
        tree_sha=tree_sha,
        base_image_digest=base_image_digest,
        build_config_uri="cas://invocation/1",
        material_digests=material_digests,
        hermetic=hermetic,
        seq=_seq(tree_sha),
    )


def _seq(run_id: str = "tree-abc"):
    from foundry.state.seq import Seq

    return Seq(
        run_id=run_id,
        emission_epoch=0,
        superstep_index=0,
        node_id="trusted_builder",
        local=0,
    )


_COMPONENTS = (
    SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT_A, license="MIT"),
    SBOMComponent(name="libbar", version="4.5.6", digest=_MAT_B, license="Apache-2.0"),
)


@pytest.fixture
def signer() -> ZoneBSigner:
    return ZoneBSigner(KEY, KEY_ID)


@pytest.fixture
def builder(signer: ZoneBSigner) -> TrustedBuilder:
    return TrustedBuilder(signer, builder_id=BUILDER_ID)


@pytest.fixture
def verifier() -> DevGateWriteVerifier:
    return DevGateWriteVerifier(KEY, KEY_ID, GlobalRecords())


# ---- 1. hermetic / reproducible / deterministic --------------------------------


def test_build_produces_full_output(builder: TrustedBuilder) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    assert isinstance(out, BuildOutput)
    assert out.result.subject_digest.startswith("sha256:")
    assert out.result.builder_id == BUILDER_ID
    assert out.result.hermetic is True
    assert out.provenance.hermetic is True


def test_same_inputs_reproducible(builder: TrustedBuilder) -> None:
    a = builder.build(make_request(), components=_COMPONENTS)
    b = builder.build(make_request(), components=_COMPONENTS)
    # Reproducible: byte-identical subject digest AND provenance identity.
    assert a.result.subject_digest == b.result.subject_digest
    assert a.provenance.content_identity() == b.provenance.content_identity()
    assert a.result.provenance_uri == b.result.provenance_uri
    assert a.sbom.sbom_uri == b.sbom.sbom_uri
    assert a.signed_verdict.runner_attestation == b.signed_verdict.runner_attestation


def test_different_material_changes_subject(builder: TrustedBuilder) -> None:
    a = builder.build(make_request(), components=_COMPONENTS)
    b = builder.build(
        make_request(material_digests=(_MAT_A, "sha256:" + "9" * 64)),
        components=_COMPONENTS,
    )
    # Flipping a material digest re-keys the subject AND the provenance identity.
    assert a.result.subject_digest != b.result.subject_digest
    assert a.provenance.content_identity() != b.provenance.content_identity()


def test_subject_is_pure_function_of_pinned_inputs(builder: TrustedBuilder) -> None:
    # Changing source/base/tree each re-derives a distinct subject digest.
    base = builder.build(make_request(), components=_COMPONENTS).result.subject_digest
    for kw in (
        {"source_digest": "sha256:" + "1" * 64},
        {"base_image_digest": "sha256:" + "2" * 64},
        {"tree_sha": "tree-other"},
    ):
        other = builder.build(make_request(**kw), components=_COMPONENTS)  # type: ignore[arg-type]
        assert other.result.subject_digest != base


# ---- 2. fail-closed on un-pinned inputs (no network = pinned-only) --------------


def test_unpinned_source_refused(builder: TrustedBuilder) -> None:
    with pytest.raises(NonHermeticBuildError):
        builder.build(make_request(source_digest="HEAD"), components=_COMPONENTS)


def test_unpinned_base_image_refused(builder: TrustedBuilder) -> None:
    with pytest.raises(NonHermeticBuildError):
        builder.build(
            make_request(base_image_digest="latest"), components=_COMPONENTS
        )


def test_unpinned_material_refused(builder: TrustedBuilder) -> None:
    with pytest.raises(NonHermeticBuildError):
        builder.build(
            make_request(material_digests=(_MAT_A, "floating-tag")),
            components=_COMPONENTS,
        )


def test_non_hermetic_request_refused(builder: TrustedBuilder) -> None:
    with pytest.raises(NonHermeticBuildError):
        builder.build(make_request(hermetic=False), components=_COMPONENTS)


def test_unpinned_sbom_component_refused(builder: TrustedBuilder) -> None:
    bad = (SBOMComponent(name="libfoo", version="1.2.3", digest="unpinned"),)
    with pytest.raises(NonHermeticBuildError):
        builder.build(make_request(), components=bad)


def test_is_pinned_predicate() -> None:
    assert is_pinned("sha256:" + "a" * 64) is True
    assert is_pinned("sha256:" + "A" * 64) is False  # uppercase not canonical
    assert is_pinned("sha256:short") is False
    assert is_pinned("HEAD") is False
    assert is_pinned("") is False


# ---- 3. provenance is input-bound + the signed attestation verifies -------------


def test_provenance_binds_materials_and_subject(builder: TrustedBuilder) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    prov = out.provenance
    # Materials carry the pinned source + base image + declared material digests.
    digests = set(prov.material_digests())
    assert {_SRC, _BASE, _MAT_A, _MAT_B} <= digests
    assert prov.binds_subject(out.result.subject_digest)
    assert prov.builder_id == BUILDER_ID


def test_signed_attestation_verifies_for_genuine_provenance(
    builder: TrustedBuilder, verifier: DevGateWriteVerifier
) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    assert (
        verifier.verify_provenance_binding(out.signed_verdict, out.provenance) is True
    )


def test_signed_attestation_fails_for_tampered_subject(
    builder: TrustedBuilder, verifier: DevGateWriteVerifier
) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    tampered = out.provenance.model_copy(
        update={"subject_digest": "sha256:" + "f" * 64}
    )
    assert tampered.content_identity() != out.provenance.content_identity()
    assert verifier.verify_provenance_binding(out.signed_verdict, tampered) is False


def test_signed_attestation_fails_for_tampered_material(
    builder: TrustedBuilder, verifier: DevGateWriteVerifier
) -> None:
    genuine = builder.build(make_request(), components=_COMPONENTS)
    # The provenance from a DIFFERENT material set must not verify the genuine sig.
    other = builder.build(
        make_request(material_digests=(_MAT_A, "sha256:" + "7" * 64)),
        components=_COMPONENTS,
    )
    assert (
        verifier.verify_provenance_binding(
            genuine.signed_verdict, other.provenance
        )
        is False
    )


def test_build_result_carries_signature_and_subject(builder: TrustedBuilder) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    assert out.result.signature == out.signed_verdict.runner_attestation
    assert out.result.signature is not None
    assert out.result.subject_digest == out.provenance.subject_digest
    assert out.result.provenance_uri.startswith("cas://provenance/")


# ---- 4. SBOM lists the resolved components -------------------------------------


def test_sbom_lists_resolved_components(builder: TrustedBuilder) -> None:
    out = builder.build(make_request(), components=_COMPONENTS)
    assert out.sbom.components == _COMPONENTS
    assert out.sbom.fully_resolved is True
    assert out.sbom.unpinned_components() == ()
    assert out.sbom.tree_sha == "tree-abc"
    assert out.sbom.sbom_uri.startswith("cas://sbom/")
    assert out.result.sbom_uri == out.sbom.sbom_uri
