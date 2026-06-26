"""The strongly-hermetic, SLSA-L3 trusted builder (T-M4-06).

A :class:`TrustedBuilder` turns a pinned :class:`BuildRequest` into a
:class:`BuildResult` by performing a HERMETIC build: a PURE FUNCTION of its
content-addressed inputs. It runs NO agent code — the builder is trusted infra
(zone B), it holds the dev-grade signing root, and it never reaches the network.

Hermeticity (governance.md §4 — the boundary is on the build INPUTS, not just
the environment) is enforced structurally, not asserted:

- **Pinned-inputs-only.** Every input the subject is derived from — the source
  commit, the base image, and every material — must be content-addressed
  (``sha256:<64-hex>``). A non-pinned / un-digested input is REFUSED at the door
  (:class:`NonHermeticBuildError`); we never start a build we cannot reproduce.
- **No network.** The builder takes ONLY digest-addressed inputs and emits a
  digest derived from them; there is no egress seam in this module at all (no
  http client, no resolver, no clock) — un-pinned inputs are the only way to
  pull in something unnamed, and those fail closed. So "no network" is a
  consequence of "pinned-inputs-only", not a runtime guard that could be bypassed.
- **Reproducible / deterministic.** The produced ``subject_digest`` is
  ``content_digest`` over the canonical, sorted tuple of pinned inputs. Same
  pinned inputs ⇒ byte-identical subject digest ⇒ identical
  :class:`SLSAProvenance` identity. Flipping ANY material digest re-keys the
  subject and the provenance (the predicate's INPUT-BINDING, security.py).

What it produces (the input-bound evidence the deploy-time admission controller,
T-M4-07, re-verifies):

- the authoritative :class:`SBOM` (resolved components + versions + digests);
- the input-bound :class:`SLSAProvenance` predicate (builder id, build type, the
  pinned materials' digests, the produced subject digest, source/tree);
- a build verdict (:class:`GateResult`) whose attestation is bound INTO the
  provenance via ``ZoneBSigner.with_provenance_binding`` — verifiable for the
  genuine provenance and FAILING for any tampered one.

DEV-GRADE ROOT (implementation-plan.md §0.3). The LOGIC and interfaces here are
real and correct: pinned-input enforcement, deterministic subject derivation,
SBOM resolution binding, the input-bound signed provenance. The cryptographic
ROOT is dev-grade — ``builder_id`` is a local stand-in identity, the signer is
an HMAC key (not hardware-backed / keyless-OIDC), and ``hermetic`` asserts the
LOGICAL isolation posture rather than a real air-gapped SLSA-L3 build cage. Real
KMS / OIDC federation / hardware root / air-gapped builders are an ops
deployment task; this discipline is what they harden, not replace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from foundry.canonical import content_digest
from foundry.gates.attestation import ZoneBSigner
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.security import (
    SBOM,
    Material,
    SBOMComponent,
    SLSAProvenance,
)
from foundry.schemas.trust_root import (
    BuildRequest,
    BuildResult,
)
from foundry.state.seq import Seq

# A content address is "sha256:" + 64 lowercase hex chars. Anything else is an
# un-pinned input (a tag, a floating ref, a bare name) — fail-closed.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# CAS handle prefixes for the out-of-band predicate / SBOM bodies (DS9). The
# builder mints handles; the bodies live in the CAS / artifact store.
_PROVENANCE_PREFIX = "cas://provenance/"
_SBOM_PREFIX = "cas://sbom/"


def is_pinned(digest: str) -> bool:
    """True iff ``digest`` is a content address (``sha256:<64-hex>``).

    The single hermeticity predicate: an input that is not content-addressed is
    not reproducibly pinned, so it cannot enter a hermetic build."""
    return bool(_DIGEST_RE.match(digest))


class NonHermeticBuildError(ValueError):
    """A build input is not pinned/digest-addressed — the build is REFUSED.

    Raised fail-closed before any work: a hermetic build is a pure function of
    content-addressed inputs, so an un-pinned source/base-image/material/SBOM
    component (or ``request.hermetic is False``) means the build is not
    reproducible and must not run (governance.md §4; plan §0.3)."""


@dataclass(frozen=True)
class BuildOutput:
    """The trusted builder's full output bundle.

    ``result`` is the channel/persisted :class:`BuildResult` record (subject
    digest + handles + signature). ``provenance`` and ``sbom`` are the genuine
    bodies the handles point at — the EXACT objects admission (T-M4-07) must be
    re-presented to re-verify the signature against; ``signed_verdict`` is the
    build :class:`GateResult` carrying the provenance-bound attestation.

    Bodies travel here (not inline on ``BuildResult``) so a caller can persist
    them to the CAS and re-derive the same handles, mirroring DS9/L4."""

    result: BuildResult
    provenance: SLSAProvenance
    sbom: SBOM
    signed_verdict: GateResult


class TrustedBuilder:
    """Hermetic SLSA-L3 trusted builder (governance.md §4; plan §0.3).

    Holds the dev-grade zone-B signing root and a stable ``builder_id``. Runs NO
    agent code. ``build`` is the only entry point; it is deterministic — the same
    :class:`BuildRequest` + resolved SBOM components yield a byte-identical
    :class:`BuildOutput` (subject digest, provenance identity, signature)."""

    def __init__(self, signer: ZoneBSigner, *, builder_id: str) -> None:
        self._signer = signer
        self._builder_id = builder_id

    # -- public API -----------------------------------------------------------

    def build(
        self,
        request: BuildRequest,
        *,
        components: tuple[SBOMComponent, ...] = (),
        gate_result_uris: tuple[str, ...] = (),
        scanner_config_rev: str | None = None,
        trust_store_rev: str | None = None,
        seq: Seq | None = None,
    ) -> BuildOutput:
        """Perform a hermetic build of the request's subject.

        ``components`` is the resolved component inventory the build pinned (in a
        production builder this is re-derived from the lockfile inside the
        hermetic cage; here it is the authoritative resolution handed to the
        trusted builder). Each component MUST itself be digest-pinned — an
        unpinned component fails closed (no unpinned components, §4).

        Steps: (1) refuse a non-hermetic / un-pinned request fail-closed; (2)
        derive the deterministic ``subject_digest`` from exactly the pinned
        inputs; (3) build the authoritative SBOM; (4) build the input-bound
        SLSA provenance predicate; (5) sign a build verdict bound INTO that
        provenance; (6) return the :class:`BuildResult` + bodies + signed verdict.
        """
        build_seq = seq or self._default_seq(request)
        materials = self._pinned_materials(request)  # fail-closed on un-pinned
        self._check_components_pinned(components)

        subject_digest = self._derive_subject_digest(request, materials)
        subject_name = f"{request.build_type}@{request.tree_sha}"

        sbom = SBOM(
            tree_sha=request.tree_sha,
            sbom_uri="",  # filled below once we know the body's content identity
            components=components,
            fully_resolved=True,
            seq=build_seq,
        )
        sbom = sbom.model_copy(update={"sbom_uri": self._sbom_handle(sbom)})

        provenance = SLSAProvenance(
            build_type=request.build_type,
            builder_id=self._builder_id,
            invocation_uri=request.build_config_uri,
            source_uri=request.source_uri,
            source_digest=request.source_digest,
            tree_sha=request.tree_sha,
            subject_name=subject_name,
            subject_digest=subject_digest,
            materials=materials,
            gate_result_uris=gate_result_uris,
            scanner_config_rev=scanner_config_rev,
            trust_store_rev=trust_store_rev,
            hermetic=True,
            seq=build_seq,
        )
        provenance_uri = self._provenance_handle(provenance)

        signed_verdict = self._sign_build(
            provenance=provenance,
            scanner_config_rev=scanner_config_rev,
            seq=build_seq,
        )

        result = BuildResult(
            subject_digest=subject_digest,
            tree_sha=request.tree_sha,
            source_digest=request.source_digest,
            builder_id=self._builder_id,
            provenance_uri=provenance_uri,
            sbom_uri=sbom.sbom_uri,
            signature=signed_verdict.runner_attestation,
            hermetic=True,
            seq=build_seq,
        )
        return BuildOutput(
            result=result,
            provenance=provenance,
            sbom=sbom,
            signed_verdict=signed_verdict,
        )

    # -- hermeticity enforcement (fail-closed) --------------------------------

    def _pinned_materials(self, request: BuildRequest) -> tuple[Material, ...]:
        """Resolve the pinned input set, REFUSING any un-pinned input.

        The source commit, the base image, and every declared material digest
        must be content-addressed. A floating ref / bare name / non-hermetic
        request raises :class:`NonHermeticBuildError` BEFORE any build work — the
        build never starts unless it is reproducible from pinned inputs (§4)."""
        if not request.hermetic:
            raise NonHermeticBuildError(
                f"build for tree {request.tree_sha!r} is not marked hermetic — "
                "refusing (a non-hermetic build is not reproducible, §4)"
            )

        materials: list[Material] = []
        # Source commit + base image are themselves pinned build inputs.
        for name, uri, digest in (
            ("source", request.source_uri, request.source_digest),
            ("base_image", "oci://base-image", request.base_image_digest),
        ):
            if not is_pinned(digest):
                raise NonHermeticBuildError(
                    f"un-pinned {name} input {digest!r} for tree "
                    f"{request.tree_sha!r}: a hermetic build accepts only "
                    "content-addressed (sha256:<hex>) inputs, refusing (§4)"
                )
            materials.append(Material(uri=uri, digest=digest))

        for i, digest in enumerate(request.material_digests):
            if not is_pinned(digest):
                raise NonHermeticBuildError(
                    f"un-pinned material[{i}] {digest!r} for tree "
                    f"{request.tree_sha!r}: every material must be "
                    "digest-addressed, refusing the build (§4)"
                )
            materials.append(Material(uri=f"material://{i}", digest=digest))

        return tuple(materials)

    @staticmethod
    def _check_components_pinned(components: tuple[SBOMComponent, ...]) -> None:
        """Refuse an unpinned SBOM component (no unpinned components, §4)."""
        unpinned = [c.name for c in components if not is_pinned(c.digest)]
        if unpinned:
            raise NonHermeticBuildError(
                "un-pinned SBOM component(s) "
                f"{unpinned!r}: the authoritative SBOM admits no unpinned "
                "components, refusing the build (§4)"
            )

    # -- deterministic derivation ---------------------------------------------

    @staticmethod
    def _derive_subject_digest(
        request: BuildRequest, materials: tuple[Material, ...]
    ) -> str:
        """The reproducible subject content address: a PURE function of the
        pinned inputs.

        ``content_digest`` over the canonical (sorted-key) tuple of every pinned
        input — build type, source/tree, base image, and the ORDERED material
        digests. Same pinned inputs ⇒ identical hex ⇒ identical subject (and
        thus identical provenance identity). Flipping ANY material digest changes
        this hex, which re-keys the subject AND the provenance predicate."""
        body = content_digest(
            {
                "build_type": request.build_type,
                "source_digest": request.source_digest,
                "tree_sha": request.tree_sha,
                "base_image_digest": request.base_image_digest,
                "materials": [m.digest for m in materials],
            }
        )
        return f"sha256:{body}"

    # -- signing (dev-grade zone-B root) --------------------------------------

    def _sign_build(
        self,
        *,
        provenance: SLSAProvenance,
        scanner_config_rev: str | None,
        seq: Seq,
    ) -> GateResult:
        """Attest the build by binding the provenance INTO a build verdict.

        Mints a PASS build :class:`GateResult` keyed to the integrated
        ``tree_sha`` and binds the provenance identity into its attestation via
        ``ZoneBSigner.with_provenance_binding`` — so the signature attests the
        EXACT build (subject/builder/materials). Verifies with
        ``DevGateWriteVerifier.verify_provenance_binding(verdict, provenance)``;
        re-presenting a tampered provenance recomputes a different MAC and fails.
        Dev-grade HMAC root (plan §0.3)."""
        verdict = GateResult(
            name="build",
            status=GateStatus.PASS,
            decision_deterministic=True,
            execution_deterministic=True,
            verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
            scanner_config_rev=scanner_config_rev,
            results_digest=provenance.subject_digest,
            run_against=provenance.tree_sha,
            seq=seq,
        )
        return self._signer.with_provenance_binding(verdict, provenance)

    # -- CAS handle minting (DS9) ---------------------------------------------

    @staticmethod
    def _provenance_handle(provenance: SLSAProvenance) -> str:
        """Content-addressed handle for the predicate body — keyed on its
        identity so re-deriving the same provenance yields the same handle."""
        return f"{_PROVENANCE_PREFIX}{provenance.content_identity()}"

    @staticmethod
    def _sbom_handle(sbom: SBOM) -> str:
        """Content-addressed handle for the SBOM body. Computed over the SBOM
        WITHOUT its own ``sbom_uri`` (which is filled from this), so the handle
        is a stable function of the resolved inventory."""
        keyed = sbom.model_copy(update={"sbom_uri": ""})
        return f"{_SBOM_PREFIX}{keyed.content_identity()}"

    @staticmethod
    def _default_seq(request: BuildRequest) -> Seq:
        """A deterministic build seq derived from the request when the caller
        does not supply one — keeps the builder usable standalone in tests while
        preserving determinism (same request ⇒ same seq)."""
        return Seq(
            run_id=request.tree_sha,
            emission_epoch=0,
            superstep_index=0,
            node_id="trusted_builder",
            local=0,
        )
