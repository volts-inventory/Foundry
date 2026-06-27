"""Deploy-time admission control — the fail-closed trust-root deploy gate (T-M4-07).

This is the gate that decides whether an artifact may reach production. It is the
direct embodiment of governance.md §5: *signature ≠ safety, so verify on
admission*. A signed artifact proves provenance, not that deployment is
authorized — so the non-agent :class:`AdmissionController` RE-VERIFIES, at deploy
time, for the EXACT subject digest:

1. **Provenance bound (input-binding).** The re-presented :class:`SLSAProvenance`
   names exactly this ``subject_digest`` (``prov.binds_subject(digest)``). The
   integrated ``tree_sha`` the gate-state is keyed to is read FROM the provenance,
   never from the caller — so a verdict/provenance built for a DIFFERENT digest,
   re-presented for this subject, is rejected (the provenance won't bind it). A
   tampered/replayed provenance also fails the signature re-check below (its
   ``content_identity()`` changes).
2. **Signature re-verified.** The build provenance attestation re-verifies under
   the trusted verifier via ``verify_provenance_binding(build_verdict, prov)``
   (re-presenting the EXACT provenance), and every required gate verdict
   re-verifies via ``verify_attestation`` — a forged/unsigned verdict yields
   ``DENY``.
3. **Gate-state complete + for THIS digest/tree (across BOTH trees).** Every
   gate in ``required_gates`` is attested ``PASS`` AND bound to ITS expected
   ``run_against`` tree. By default that expected tree is the provenance's exact
   ``tree_sha``; but a release may span MULTIPLE materialized trees — the
   terminal Spec/Build/Test gates ran on the terminal integrated tree while the
   assurance gates (Security/Audit/...) ran on a DISTINCT re-hashed tree (the
   assurance materializer augments + re-hashes the augmented tree, AW2). The
   caller may therefore pass ``expected_gate_trees`` — a per-gate name→tree
   mapping pinning EACH required gate to the tree it is supposed to have run on
   (the terminal gates to the terminal tree, the assurance gates to the
   assurance/provenance tree). Admission then CRYPTOGRAPHICALLY re-verifies EVERY
   required gate (signature + PASS + bound-to-its-expected-tree), so the
   trust-root release decision covers the terminal Spec/Build/Test gates too —
   not just the assurance group. A missing required gate, a non-PASS gate, or a
   gate whose verdict is bound to a DIFFERENT tree than expected ⇒ ``DENY``
   (fail-closed: never advance on an absent verdict; never accept a verdict for
   the wrong tree). With no mapping (single-tree caller) every gate is expected
   on the provenance's ``tree_sha`` — the original behavior, unchanged.
4. **Not disqualified / not fork-divergent.** ``records.admission_look(digest)``
   must be :data:`AdmissionState.ADMISSIBLE` — anything else (an explicit
   cross-fork digest-disqualification, or a stable-FAIL latch on the digest)
   yields ``DENY``. This is the fork-divergence defense (gov §5): a forged or
   laundered fork re-presenting a disqualified digest under a fresh ``run_id``
   still reads DISQUALIFIED, so it can never be admitted.

ANY missing/failed check ⇒ ``DENY`` (fail-closed default — unknown is DENY, never
silent admit). The controller runs NO agent code; it is trusted infra and is
deterministic — the same inputs always yield the same :class:`AdmissionDecision`,
which records the EXACT digest, the integrated tree, the grounds it verified, and
the deciding deny reason.

DEV-GRADE ROOT (implementation-plan.md §0.3). The re-verification LOGIC here — the
fail-closed ordering, the input-binding to the exact digest/tree, the signature +
gate-state + disqualification re-checks — is REAL and CORRECT. Its cryptographic
ROOT is dev-grade: the signatures it re-verifies are HMAC under a locally-held
zone-B key (not hardware/keyless-OIDC), and the revocation/IdP standing the
``signature_verified`` bit stands for is a local stand-in. The discipline is real;
only the root is dev-grade.

The deployer (T-M4-08) deploys ONLY an admitted decision; Autonomous mode
(T-M4-10) uses admission as the SOLE release gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.trust_root import (
    AdmissionDecision,
    AdmissionVerdict,
)
from foundry.state.global_records import AdmissionState

if TYPE_CHECKING:
    from collections.abc import Mapping

    from foundry.gates.attestation import DevGateWriteVerifier
    from foundry.schemas.governance import EvidencePack
    from foundry.schemas.security import SLSAProvenance
    from foundry.state.global_records import GlobalRecordsView
    from foundry.state.seq import Seq


# Deny reason codes (stable strings — the deciding ground, recorded on the
# AdmissionDecision.reason for the deployer / WORM audit). "ok" marks an ADMIT.
_OK = "ok"
_R_PROVENANCE_UNBOUND = "provenance_not_bound_to_subject"
_R_SIG_INVALID = "build_signature_invalid"
_R_GATE_MISSING = "required_gate_missing"
_R_GATE_NOT_PASS = "required_gate_not_pass"
_R_GATE_WRONG_TREE = "required_gate_wrong_tree"
_R_GATE_SIG_INVALID = "required_gate_signature_invalid"
_R_DISQUALIFIED = "stable_fail_latched_or_disqualified"
_R_SCANNER_CONFIG_MISMATCH = "scanner_config_rev_mismatch"

# Deny reasons that indicate a FORGERY / TAMPER / cross-tree replay (not a benign
# gate failure): a forged/unsigned signature, a verdict bound to the wrong tree, a
# provenance that doesn't bind the subject, or a tampered scanner config. On such a
# deny the caller disqualifies the digest cross-fork (gov §5 fork-divergence
# defense) so a sibling fork re-presenting it under a fresh run_id is also denied.
# A plain `_R_GATE_NOT_PASS` (an honest failing gate) is NOT here — the stable-FAIL
# latch handles that; we do not poison a digest for an honest failure.
FORGERY_DENY_REASONS: frozenset[str] = frozenset(
    {
        _R_PROVENANCE_UNBOUND,
        _R_SIG_INVALID,
        _R_GATE_WRONG_TREE,
        _R_GATE_SIG_INVALID,
        _R_SCANNER_CONFIG_MISMATCH,
    }
)


@dataclass(frozen=True)
class AdmissionController:
    """Non-agent, deterministic, fail-closed deploy-time admission controller.

    Trusted infra (zone B): holds no agent code and reaches no network. ``admit``
    is the only entry point; it is a pure function of its re-presented evidence +
    the cross-fork records view + the trusted verifier, so the same inputs always
    yield the same :class:`AdmissionDecision` (governance.md §5).

    ``builder_id`` is the trusted builder identity admission expects to have
    produced the artifact (dev-grade root, §0.3); a provenance from a different
    builder is rejected at the binding check. ``require_hermetic`` (default True)
    refuses a non-hermetically-built artifact — a fail-closed admission input (§4).
    """

    builder_id: str
    require_hermetic: bool = True

    def admit(
        self,
        subject_digest: str,
        *,
        provenance: SLSAProvenance,
        build_verdict: GateResult,
        gate_results: tuple[GateResult, ...],
        required_gates: tuple[str, ...],
        records: GlobalRecordsView,
        verifier: DevGateWriteVerifier,
        seq: Seq,
        expected_gate_trees: Mapping[str, str] | None = None,
        evidence_pack: EvidencePack | None = None,
        scanner_config_rev: str | None = None,
        gate_state_uri: str | None = None,
    ) -> AdmissionDecision:
        """Decide whether ``subject_digest`` may be admitted to deploy.

        Returns an :class:`AdmissionDecision` recording the exact digest, the
        integrated tree the gate-state is keyed to, which grounds it verified, and
        — on a DENY — the deciding reason. Admits ONLY when ALL four checks hold
        for the EXACT ``subject_digest``; ANY missing/failed check ⇒ DENY
        (fail-closed). See the module docstring for the check semantics.

        ``required_gates`` is the gate set the mode/tier demands PASS (e.g.
        Spec/Build/Test/Security/...). ``gate_results`` is the WORM-anchored,
        trusted-runner-attested gate-state for this digest; ``build_verdict`` is
        the build :class:`GateResult` whose provenance-bound attestation grounds
        ``provenance``. ``records.admission_look`` is the cross-fork
        disqualification / stable-FAIL query (T-M4-02).

        ``expected_gate_trees`` (optional) pins EACH required gate to the
        ``run_against`` tree it is supposed to have run on. When a release spans
        MULTIPLE materialized trees — terminal Spec/Build/Test on the terminal
        integrated tree, assurance gates on a DISTINCT re-hashed tree (AW2) —
        the caller maps each required gate to its tree so admission re-verifies
        ALL of them (not just the assurance group) against the tree they belong
        to. A gate absent from the mapping defaults to the provenance's
        ``tree_sha`` (so a single-tree caller passes nothing and every gate is
        checked against the provenance tree — original behavior). The provenance
        still binds the final integrated subject; this only widens the gate-state
        re-verification to honestly cover the terminal gates' distinct tree.
        """
        tree_sha = provenance.tree_sha

        # The decision skeleton; we flip grounds True as each check passes and
        # short-circuit to a DENY on the first failure (fail-closed).
        verified_signature = False
        verified_gates = False
        latched = False

        def deny(reason: str) -> AdmissionDecision:
            return AdmissionDecision(
                subject_digest=subject_digest,
                tree_sha=tree_sha,
                verdict=AdmissionVerdict.DENY,
                reason=reason,
                signature_verified=verified_signature,
                all_required_gates_pass=verified_gates,
                required_gates=required_gates,
                gate_state_uri=gate_state_uri,
                scanner_config_rev=provenance.scanner_config_rev,
                stable_fail_latched=latched,
                seq=seq,
            )

        # -- 1. Provenance bound to the EXACT subject (input-binding) ----------
        # The provenance must name exactly THIS digest, be from the expected
        # trusted builder, and (if required) record a hermetic build. A
        # verdict/provenance built for a DIFFERENT digest cannot pass: the
        # provenance won't bind this subject. This also fixes `tree_sha` to the
        # provenance's own tree, so the gate-state check below is keyed to the
        # tree the provenance attests — never a caller-supplied one.
        if (
            not provenance.binds_subject(subject_digest)
            or provenance.builder_id != self.builder_id
            or (self.require_hermetic and not provenance.hermetic)
        ):
            return deny(_R_PROVENANCE_UNBOUND)

        # -- scanner-config pin: provenance rev must match the pinned hash -----
        # (gov §5: "the scanner-config revision matches the pinned hash"). Only
        # enforced when the caller pins one; a tampered/weakened config rev DENYs.
        if scanner_config_rev is not None and provenance.scanner_config_rev != scanner_config_rev:
            return deny(_R_SCANNER_CONFIG_MISMATCH)

        # -- 2a. Build provenance attestation re-verifies under the verifier ---
        # Re-present the EXACT provenance: a forged/unsigned build signature, or a
        # tampered provenance (different content_identity()), recomputes a
        # different expected MAC and fails. This is the §5 "signature verifies"
        # check, bound to this build's subject/builder/materials.
        if not verifier.verify_provenance_binding(build_verdict, provenance):
            return deny(_R_SIG_INVALID)
        verified_signature = True

        # -- 3. Gate-state: every required gate attested PASS for ITS tree -----
        # Index the presented gate-state by name. A required gate that is absent,
        # not PASS, bound to a DIFFERENT tree than expected, or whose attestation
        # does not re-verify ⇒ DENY. Fail-closed: never advance on a
        # missing/forged gate. EACH gate's expected tree is the provenance's
        # `tree_sha` by default, or the per-gate tree the caller pinned in
        # `expected_gate_trees` (a release that spans the terminal + assurance
        # trees; AW2). This is what makes admission cryptographically re-verify
        # the TERMINAL Spec/Build/Test gates too — bound to their own tree —
        # rather than resting them on the upstream emit guard.
        trees = expected_gate_trees or {}
        by_name = self._latest_by_name(gate_results)
        for name in required_gates:
            g = by_name.get(name)
            if g is None:
                return deny(_R_GATE_MISSING)
            # input-bound: the verdict must attest ITS expected integrated tree,
            # not a different tree re-presented for this subject.
            if g.run_against != trees.get(name, tree_sha):
                return deny(_R_GATE_WRONG_TREE)
            if g.status is not GateStatus.PASS:
                return deny(_R_GATE_NOT_PASS)
            # re-verify the trusted-runner attestation: a forged/unsigned verdict
            # never grounds admission.
            if not verifier.verify_attestation(g):
                return deny(_R_GATE_SIG_INVALID)
        verified_gates = True

        # -- 4. Not disqualified / not fork-divergent --------------------------
        # The cross-fork, monotonic WORM query (T-M4-02): a digest with a stable
        # FAIL latch OR an explicit cross-fork disqualification is not admissible,
        # regardless of any later PASS in the live gates reducer. A forged or
        # laundered fork re-presenting a disqualified digest under a fresh run_id
        # still reads DISQUALIFIED here.
        if records.admission_look(subject_digest) is not AdmissionState.ADMISSIBLE:
            latched = True
            return deny(_R_DISQUALIFIED)

        # -- 4b. Stable-FAIL latch is keyed on the TREE, not the subject -------
        # The runner latches stable FAIL on `run_against` (the integrated tree sha),
        # a DIFFERENT namespace from `subject_digest` — so check #4's subject query
        # alone would miss a stably-FAILed tree re-presented with a fresh PASS (the
        # newest-by-Seq verdict check #3 reads). Consult the latch under every tree
        # this release rests on (the provenance tree + any distinct per-gate tree).
        # This is what actually upholds the §5 "a later PASS can't mask a stable
        # FAIL at deploy" invariant the check #4 docstring claims.
        for expected_tree in {tree_sha, *trees.values()}:
            if records.admission_look(expected_tree) is not AdmissionState.ADMISSIBLE:
                latched = True
                return deny(_R_DISQUALIFIED)

        # All four checks held for the EXACT subject_digest ⇒ ADMIT.
        return AdmissionDecision(
            subject_digest=subject_digest,
            tree_sha=tree_sha,
            verdict=AdmissionVerdict.ADMIT,
            reason=_OK,
            signature_verified=True,
            all_required_gates_pass=True,
            required_gates=required_gates,
            gate_state_uri=gate_state_uri,
            scanner_config_rev=provenance.scanner_config_rev,
            stable_fail_latched=False,
            seq=seq,
        )

    @staticmethod
    def _latest_by_name(gate_results: tuple[GateResult, ...]) -> dict[str, GateResult]:
        """Index gate-state by gate name, keeping the newest-by-``Seq`` per name.

        Mirrors the live `gates` reducer's latest-by-Seq tiebreak so a re-emitted
        verdict (resume) coalesces deterministically; the disqualification check
        (#4) is what holds a stable-FAIL'd digest down regardless of any later
        PASS, so reading the newest PASS here is sound."""
        latest: dict[str, GateResult] = {}
        for g in gate_results:
            cur = latest.get(g.name)
            if cur is None or g.seq > cur.seq:
                latest[g.name] = g
        return latest
