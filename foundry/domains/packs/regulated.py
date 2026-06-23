"""The REGULATED domain pack (fintech PCI/SOX · healthcare HIPAA/FDA · IEC 62304).

This is the first-party pack that makes the general factory **safe by construction**
for regulated/safety-critical software (docs/domains.md §5 "Regulated /
safety-critical"). It implements :class:`~foundry.domains.contract.DomainPackContract`
(via :class:`~foundry.domains.contract.BaseDomainPack`'s safe defaults) and overrides
only the four hooks the regulated spec tightens — registering by capability through
:class:`~foundry.domains.registry.PackRegistry` with **NO core edit** (the open/closed
seam, domains.md §4).

Every hook return is a MONOTONE policy whose ``tighten`` join is strictest-wins and
whose ``relaxes`` predicate is ``False`` for these contributions — the pack can only
TIGHTEN (domains.md §4, governance.md §1e/§5). The four overrides:

* :meth:`mode_tier_policy` (Hook 4, Regulated-F1/F5) — VETO ``autonomous`` AND the
  auto-approve ``assistant`` mode so the effective release mode can NEVER be hands-off
  for a regulated request; force-escalate ``min_tier`` to HIGH (a one-line APR/dosing
  change is not Low — the regulatory-significance axis); force-on a named human
  accountable approver + two-person sign-off at Release (segregation of duties).
* :meth:`approver_of_record` (Hook 5, Regulated-F2) — bind a class-4 human
  approver-of-record to the regulated release artifact classes (design / test_protocol
  / risk_analysis / validation_report / release). No recorded human approver ⇒ no
  release (fail-closed); an agent-identity / Autonomous / self-judge approver is
  rejected downstream.
* :meth:`assign_verdict_class` (Hook 1, Regulated-F3) — mark the regulated release /
  sign-off / validation keys as **class 4 (human-witnessed)** so they require the
  approver-of-record and Autonomous/self-judge is UNREACHABLE for them (a green
  machine gate can never satisfy a class-4 key — :func:`foundry.domains.verdict_class.satisfies`).
* :meth:`egress_profile` (Hook 6, Regulated-F4) — restrict egress: no third-party
  model/trace egress and no real PHI/PAN in the pipeline.

The remaining three hooks (state_inputs / compute_budget / perceptual_hitl) keep the
:class:`~foundry.domains.contract.BaseDomainPack` identity default — this pack adds
policy only on the axes the regulated spec tightens, and composes strictest-wins with
any other engaged pack (a regulated ML UI product engages all three; the joins keep the
strictest value on every axis).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.domains.contract import (
    ApproverPolicy,
    BaseDomainPack,
    EgressPolicy,
    ModeTierPolicy,
    VerdictClassPolicy,
)
from foundry.domains.governance_hooks import MODE_ASSISTANT, MODE_AUTONOMOUS
from foundry.schemas.gates import VerdictClass
from foundry.schemas.governance import RiskTier

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

__all__ = [
    "REGULATED_ARTIFACT_CLASSES",
    "REGULATED_CAPABILITIES",
    "REGULATED_CLASS4_KEYS",
    "RegulatedPack",
]

# The capabilities the regulated pack engages on (the routing key, registry §4). A
# request advertising ANY of these in ``required_capabilities`` routes the pack.
REGULATED_CAPABILITIES: frozenset[str] = frozenset(
    {"regulated", "fintech", "healthcare", "finance", "phi", "pii"}
)

# The class-4 artifact classes that require a credentialed human approver-of-record
# bound into WORM + predicate (domains.md §5 Regulated-F2/F3). ``release`` is the
# release-decision class the governance hooks key on; the rest are the design-control
# artifacts (design / test protocol / risk analysis / validation report).
REGULATED_ARTIFACT_CLASSES: frozenset[str] = frozenset(
    {
        "design",
        "test_protocol",
        "risk_analysis",
        "validation_report",
        "release",
    }
)

# The gate/criterion keys the regulated pack force-grounds at class 4 (human-witnessed)
# — the release sign-off + the IQ/OQ/PQ validation verdict are on equal footing with
# runner oracles, not advisory (Regulated-F3). A class-4 key disables the self-judge /
# Autonomous path: only a named human approver-of-record satisfies it.
REGULATED_CLASS4_KEYS: frozenset[str] = frozenset(
    {
        "release",
        "sign_off",
        "validation",
        "design_review",
        "risk_analysis",
    }
)


class RegulatedPack(BaseDomainPack):
    """The regulated / safety-critical domain pack (domains.md §5).

    Overrides exactly four hooks — mode/tier veto, approver-of-record, verdict-class
    assignment, and egress profile — each TIGHTENING (never relaxing) the integrity
    floor. Engages on :data:`REGULATED_CAPABILITIES`. The other three hooks inherit
    the :class:`~foundry.domains.contract.BaseDomainPack` identity default, so the
    pack composes with any other engaged pack strictest-wins (domains.md §4).
    """

    name: str = "regulated"
    capabilities: frozenset[str] = REGULATED_CAPABILITIES

    # --- Hook 4: mode/tier veto + regulatory-significance (Regulated-F1/F5) -------
    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        """VETO Autonomous + auto-approve; raise min tier; force human + two-person.

        ``autonomous`` (trust-root auto-release) AND ``assistant`` (auto-approve-able
        Phase-1 flow) are made UNREACHABLE — a regulated request can only run Gated
        with a recorded human/release sign-off. ``min_tier`` is force-escalated to
        HIGH (the regulatory-significance axis: a one-line APR/dosing change is not
        Low, independent of diff size). ``force_human_release`` + ``two_person_release``
        force-on a named, accountable, segregated-duty human approver at Release
        (auto-approve cannot override — governance.md §1e).
        """
        del request  # engagement is by capability routing; policy is unconditional
        return ModeTierPolicy(
            vetoed_modes=frozenset({MODE_AUTONOMOUS, MODE_ASSISTANT}),
            min_tier=RiskTier.HIGH,
            force_human_release=True,
            two_person_release=True,
        )

    # --- Hook 5: per-artifact class-4 approver-of-record (Regulated-F2) -----------
    def approver_of_record(self, request: Mapping[str, Any]) -> ApproverPolicy:
        """Bind a class-4 human approver-of-record to the regulated artifact classes.

        Design / test protocol / risk analysis / validation report / release each
        require a recorded human attestation (Regulated-F2). Downstream
        (:func:`foundry.domains.governance_hooks.release_admitted`) a release is
        admitted ONLY if a recorded, approved, HUMAN sign-off binds the artifact +
        ``state_sha`` — no approver ⇒ no release; an agent-identity / Autonomous /
        self-judge approver is rejected fail-closed.
        """
        del request
        return ApproverPolicy(bound_artifact_classes=REGULATED_ARTIFACT_CLASSES)

    # --- Hook 1: verdict-class assignment (Regulated-F3) --------------------------
    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        """Force the regulated release / sign-off / validation keys to class 4.

        Class 4 (human-witnessed) requires the approver-of-record and makes
        Autonomous / self-judge / auto-approve UNREACHABLE for those keys — a green
        machine gate can never satisfy a class-4 key
        (:func:`foundry.domains.verdict_class.satisfies`). The join only ever keeps
        the STRICTER class, so this can only tighten an existing grounding.
        """
        del request
        return VerdictClassPolicy(
            classes={key: VerdictClass.HUMAN_WITNESSED for key in REGULATED_CLASS4_KEYS}
        )

    # --- Hook 6: data-egress profile (Regulated-F4) ------------------------------
    def egress_profile(self, request: Mapping[str, Any]) -> EgressPolicy:
        """Restrict egress per the regulated profile (Regulated-F4).

        No third-party model egress, no third-party trace egress, and no real
        PHI/PAN in the pipeline (synthetic / de-identified data enforced upstream).
        Each flag is an OR axis the join can only set, never clear — another engaged
        pack cannot re-permit a forbidden egress.
        """
        del request
        return EgressPolicy(
            no_third_party_model_egress=True,
            no_third_party_trace_egress=True,
            no_real_phi_pan=True,
        )
