"""Trust-root operations records (docs/governance.md §4–§5,
docs/implementation-plan.md §6 + §0.3).

The hermetic build (BuildRequest -> BuildResult), the deploy-time admission
control (AdmissionDecision), and the idempotent, rollback-capable non-agent
deploy (DeployResult). They follow the same conventions as the Layer-6
operations artifacts: FrozenModel (frozen + extra="forbid"), `seq: Seq` on every
channel record, bodies/predicates as content-addressed `*_uri`/`*_digest`
handles (DS9/L4), `default_factory` for mutable defaults (S1), integer micro-USD
for money.

DEV-GRADE-ROOTS HONESTY (plan §0.3): the build-input binding, the fail-closed
admission re-verification logic, and the idempotent rollback-capable deploy
discipline are REAL and CORRECT software. Their cryptographic ROOT is dev-grade
— the trusted builder's hardware isolation / OIDC identity, the signing-key
custody the admission `signature_verified` bit stands for, and the deployer's
least-privilege boundary are local-dev stand-ins (real KMS / SLSA-L3 hardware
root / isolated deployer is an ops deployment task), NOT a production root of
trust. The discipline is real; only the root is dev-grade.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class BuildRequest(FrozenModel):
    """A request to the trusted hermetic builder (governance.md §4).

    Names the hermetic build INPUTS the trust boundary is enforced on (not just
    the environment, §4): `source_uri`/`source_digest` (the pinned source commit),
    `tree_sha` (the integrated tree to build), the pinned `base_image_digest`, and
    the pinned `material_digests` (vendored/hash-pinned deps — no open egress,
    §4). `hermetic` asserts the no-network-egress / no-arbitrary-build-hooks
    posture (dev-grade isolation, §0.3); `build_config_uri` is the CAS handle to
    the build recipe (DS9). The builder produces a `BuildResult` whose
    `subject_digest` is a function of exactly these inputs."""

    build_type: str  # the build recipe identity (matches SLSAProvenance.build_type)
    source_uri: str  # the source repo/remote
    source_digest: str  # the pinned source commit/tree the build runs against
    tree_sha: str  # the integrated tree to build
    base_image_digest: str  # the pinned hermetic base image (no open egress, §4)
    build_config_uri: str  # CAS handle to the build invocation/config (DS9)
    material_digests: tuple[str, ...] = ()  # pinned hash-addressed input digests (§4)
    hermetic: bool = True  # no-egress / no-arbitrary-hooks posture (dev-grade, §0.3)
    seq: Seq


class BuildResult(FrozenModel):
    """The trusted hermetic builder's output (governance.md §4).

    Binds the hermetic build inputs (echoed `tree_sha` + `source_digest`) to the
    produced `subject_digest` (the artifact content address the deploy-time
    admission re-verifies, §5) and a `provenance_uri` handle to the signed
    SLSA/in-toto predicate (`SLSAProvenance`) whose materials+subject this build
    grounds. `sbom_uri` references the authoritative re-derived `SBOM`;
    `signature` is the (dev-grade, §0.3) keyless-signing attestation over the
    predicate. `hermetic` records whether the build actually ran under the
    hermetic posture — a non-hermetic build is a fail-closed admission input.

    INPUT-BINDING: `subject_digest` is the produced artifact bound to exactly the
    `tree_sha`+`source_digest` inputs; admission re-checks the signature + the
    predicate's gate-state for THIS digest/commit/builder (§5). All three travel
    on `content_identity()`."""

    subject_digest: str  # the produced artifact content address (admission re-verifies, §5)
    tree_sha: str  # the integrated tree built (echoes the request)
    source_digest: str  # the source commit built (echoes the request)
    builder_id: str  # the trusted builder identity (dev-grade root, §0.3)
    provenance_uri: str  # CAS handle to the signed SLSAProvenance predicate (DS9)
    sbom_uri: str  # CAS handle to the authoritative SBOM (DS9)
    signature: str | None = None  # keyless-signing attestation (dev-grade root, §0.3)
    hermetic: bool = True  # the build ran hermetically (fail-closed input if False, §4)
    seq: Seq


class AdmissionVerdict(StrEnum):
    """Deploy-time admission outcome (governance.md §5). Distinct from a gate
    `pass|fail|error` — admission is a non-graph deterministic policy check the
    non-agent controller runs at deploy, re-verifying signature + gate-state for
    the exact digest. `INDETERMINATE` is the explicit fail-closed state for an
    unreachable/stale revocation status (§5) — NEITHER admit NOR a gate error."""

    ADMIT = "admit"
    DENY = "deny"
    INDETERMINATE = "indeterminate"  # revocation-status unreachable/stale — fail-closed (§5)


class AdmissionDecision(FrozenModel):
    """Non-agent admission controller decision (governance.md §5: signature ≠
    safety, so verify on admission).

    Carries the EXACT digest admitted/denied (`subject_digest`), the integrated
    `tree_sha` the gate-state is keyed to, and the precise grounds it verified:
    `signature_verified` (cosign over the dev-grade root, §0.3), `gate_state_uri`
    (the WORM-anchored, hash-chained, trusted-runner-attested gate results for
    THIS digest — `required_gates` lists the names; `all_required_gates_pass`
    records that they ALL PASS, gov §1c/§5), `scanner_config_rev` (must match the
    pinned hash bound in the predicate, §5), and `stable_fail_latched` (the
    monotonic WORM-history latch — a digest with an un-overridden stable FAIL is
    disqualified regardless of any later PASS, §5). `verdict` ADMITs only when the
    signature verifies AND all required gates PASS for this exact digest AND the
    scanner-config matches AND nothing is latched; otherwise DENY (or
    INDETERMINATE on unreachable revocation). `reason` records WHY (the deciding
    grounds). Fail-closed by construction.

    DEV-GRADE ROOT (plan §0.3): the re-verification LOGIC is real; the signature
    it checks is rooted in a dev-grade key, and the revocation/IdP standing it
    asserts is a local stand-in. The fail-closed seam is the real part."""

    subject_digest: str  # the EXACT digest admission decided over (§5)
    tree_sha: str  # the integrated tree the gate-state is keyed to
    verdict: AdmissionVerdict
    reason: str  # the deciding grounds (e.g. "stable_fail_latched" / "sig_invalid" / "ok")
    signature_verified: bool = False  # cosign over the dev-grade root (§0.3/§5)
    all_required_gates_pass: bool = False  # ALL required gates PASS for THIS digest (§1c/§5)
    required_gates: tuple[str, ...] = ()  # the gate names admission required to PASS (§5)
    gate_state_uri: str | None = None  # WORM-anchored attested gate-state handle (§4/§5)
    scanner_config_rev: str | None = None  # must match the pinned predicate rev (§5)
    stable_fail_latched: bool = False  # monotonic WORM-history disqualification latch (§5)
    seq: Seq

    def admitted(self) -> bool:
        """True ONLY on an ADMIT verdict (fail-closed: DENY/INDETERMINATE are not
        admission — a missing/indeterminate ground never silently ships, §5)."""
        return self.verdict is AdmissionVerdict.ADMIT


class DeployResult(FrozenModel):
    """Idempotent, rollback-capable non-agent deploy execution record
    (governance.md §5: a separate, least-privileged, non-agent deployer ships
    only after admission).

    Distinct from `DeployPlan` (the plan + rollback procedure body the agent
    authors): this is the EXECUTION record the non-agent deployer emits, binding
    the deployed `subject_digest` (the exact admitted digest, §5) to a stable
    `deploy_id` and the process-stable `idempotency_key` (content-derived, never
    `hash()` — L7) so a resume re-verifies by key and NEVER re-fires the deploy
    (arch §5 / gov §10.7). `rollback_to_digest` is the prior-good pointer the
    deployer rolls back to; `rolled_back` flips when a rollback was executed.
    `admission_decision_uri` links the `AdmissionDecision` that authorized this
    deploy — a deploy with no admitting decision is never executed (fail-closed).

    DEV-GRADE ROOT (plan §0.3): the idempotency + rollback discipline is real; the
    least-privileged deployer's isolation boundary is an ops deployment concern."""

    deploy_id: str  # stable deploy identity
    subject_digest: str  # the exact admitted digest deployed (§5)
    idempotency_key: str  # process-stable (content_digest, never hash() — L7)
    admission_decision_uri: str  # the AdmissionDecision that authorized this (DS9)
    rollback_to_digest: str | None = None  # prior-good rollback pointer (§5)
    rolled_back: bool = False  # a rollback was executed
    deploy_plan_uri: str | None = None  # CAS handle to the DeployPlan body (DS9)
    seq: Seq
