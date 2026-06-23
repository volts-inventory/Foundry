"""Core enforcement of the composed domain-pack governance hooks (domains.md §4).

The pack *declares* policy via :mod:`foundry.domains.contract` (the monotone,
strictest-wins policy objects); the registry *composes* the engaged packs'
declarations strictest-wins (:class:`~foundry.domains.registry.ComposedPolicy`).
This module is the CORE machinery those declarations DRIVE — it ENFORCES the four
governance hooks fail-closed at the run seams (T-M5-08 wires it at triage/release):

* **Mode/tier veto** (:func:`resolve_effective_mode_tier`, domains.md §4(2,3)) —
  a vetoed mode is UNREACHABLE; the regulatory-significance axis forces the strict
  path; ``min_tier`` raises the change-risk tier. Reuses
  :func:`foundry.governance.tiering.classify_tier` for the base tier and then
  TIGHTENS it (the veto can only raise the tier / disable Autonomous, never relax).
* **Approver-of-record** (:func:`bind_approver_of_record` / :func:`release_admitted`,
  domains.md §4(4), Regulated-F2) — a regulated/class-4 artifact REQUIRES a recorded
  human approver (reusing :mod:`foundry.governance.sign_off`); no approver ⇒ no
  release. An agent-identity / Autonomous auto-approve can never satisfy it.
* **Data-egress profile** (:func:`assert_egress_within_profile`, domains.md §4(5),
  Regulated-F4) — a request's egress must be WITHIN the pack's profile; a
  third-party-model/trace-egress or real-PHI/PAN violation is denied fail-closed.
* **Mandatory perceptual HITL** (:func:`perceptual_hitl_required` /
  :func:`mark_perceptual_hitl`, domains.md §4(6), UX-F1/F2) — a class-5 / declared-
  UI request is marked as REQUIRING a non-auto-approvable visual HITL over a
  rendered-evidence bundle; it is never auto-approved.

**Load-bearing invariants (the whole module is fail-closed + tighten-only):**

* The veto can only TIGHTEN — a mode the policy vetoes is removed from the
  reachable set, and the effective tier is the STRICTER of the classifier's tier
  and the policy ``min_tier`` (never the laxer). A regulatory-significant request
  forces Gated + a human/two-person approver independent of diff size.
* No approver ⇒ no release (mirrors :func:`sign_off.release_allowed`); the bound
  approver must be a recorded human ≠ the ``autonomous`` auto-approver identity.
* An egress violation DENIES (returns a fail-closed :class:`EgressVerdict` /
  raises) — the default-permissive direction is never taken on a set forbid flag.
* A perceptual-HITL-required request can NEVER be marked auto-approved — the only
  resolution is a recorded human sign-off (the function refuses to clear it).

Pure / deterministic: every function is a pure function of its inputs (the
composed policy + an advisory request view) — no I/O, no model call, no clock —
so the verdict is stable across a kill-then-resume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.domains.contract import request_capabilities
from foundry.governance.sign_off import SignOffError, SignOffPoint, record_sign_off
from foundry.governance.tiering import classify_tier
from foundry.schemas.base import FrozenModel
from foundry.schemas.governance import RiskTier

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from foundry.domains.contract import (
        ApproverPolicy,
        EgressPolicy,
        ModeTierPolicy,
        PerceptualHITLPolicy,
    )
    from foundry.governance.human_liaison import HumanLiaison
    from foundry.schemas.governance import SignOffRecord
    from foundry.state.seq import Seq

__all__ = [
    "AUTONOMOUS_APPROVER",
    "MODE_ASSISTANT",
    "MODE_AUTONOMOUS",
    "MODE_GATED",
    "RELEASE_MODES",
    "ApproverRequirement",
    "EffectiveModeTier",
    "EgressRequest",
    "EgressVerdict",
    "GovernanceHookError",
    "PerceptualHITLRequirement",
    "assert_egress_within_profile",
    "bind_approver_of_record",
    "egress_within_profile",
    "mark_perceptual_hitl",
    "perceptual_hitl_required",
    "release_admitted",
    "resolve_effective_mode_tier",
]

# --- the release-permissiveness mode lattice (mirrors contract._MODE_*) -------
# A pack VETOES the more permissive modes by raising the floor; "strictest" = the
# LEAST permissive still reachable. These string ids mirror
# ``foundry.graph.supervisor.MODE_*`` (the veto disables MODE_AUTONOMOUS there).
MODE_AUTONOMOUS = "autonomous"  # most permissive: trust-root auto-release, no human
MODE_ASSISTANT = "assistant"  # auto-approve-able Phase-1 flow
MODE_GATED = "gated"  # least permissive: recorded human/release sign-off required

# The candidate release modes, MOST-permissive first (so the resolver's reachable
# set is deterministic and the "best still-reachable" pick is the head).
RELEASE_MODES: tuple[str, ...] = (MODE_AUTONOMOUS, MODE_ASSISTANT, MODE_GATED)

_MODE_PERMISSIVENESS: dict[str, int] = {
    MODE_AUTONOMOUS: 2,
    MODE_ASSISTANT: 1,
    MODE_GATED: 0,
}

_TIER_STRICTNESS: dict[RiskTier, int] = {
    RiskTier.LOW: 0,
    RiskTier.STANDARD: 1,
    RiskTier.HIGH: 2,
}

# The sentinel "approver" identity an Autonomous auto-approve would carry. A
# class-4 approver-of-record can NEVER be this — only a recorded human satisfies
# the binding (Regulated-F2: an agent-identity class-4 write is rejected).
AUTONOMOUS_APPROVER = "autonomous"
_NON_HUMAN_APPROVERS: frozenset[str] = frozenset(
    {AUTONOMOUS_APPROVER, "agent", "auto", "auto-approve", "self-judge", "system", ""}
)


class GovernanceHookError(RuntimeError):
    """Fail-closed: a composed governance hook was violated (egress / approver).

    Raised by the asserting enforcement helpers (:func:`assert_egress_within_profile`)
    when a request would breach a pack's tightened policy — the seam refuses it
    rather than degrade to the permissive default (domains.md §4: tighten-only,
    fail-closed).
    """


# --- (d) mode/tier veto + regulatory-significance axis -----------------------


class EffectiveModeTier(FrozenModel):
    """The resolved EFFECTIVE mode/tier for a request under a :class:`ModeTierPolicy`.

    * ``effective_mode`` — the most-permissive release mode STILL reachable after
      the veto (a vetoed mode is UNREACHABLE; if the requested mode is vetoed the
      run falls back to the strictest still-allowed mode — typically ``gated``).
    * ``reachable_modes`` — the candidate modes the veto did not remove.
    * ``effective_tier`` — the STRICTER of the deterministic classifier's tier and
      the policy ``min_tier`` (the veto can only raise it, never lower it).
    * ``force_human_release`` / ``two_person_release`` — forced-on by the policy OR
      by a regulatory-significant request (a named human accountable approver is
      mandatory; auto-approve cannot override).
    * ``regulatory_significant`` — the regulatory-significance axis fired (forces
      the strict path: Gated + human Release independent of diff size).
    * ``autonomous_reachable`` — convenience: is hands-off Autonomous still legal?
    """

    requested_mode: str
    effective_mode: str
    reachable_modes: frozenset[str]
    effective_tier: RiskTier
    classifier_tier: RiskTier
    force_human_release: bool
    two_person_release: bool
    regulatory_significant: bool
    autonomous_reachable: bool


def _is_regulatory_significant(
    request: Mapping[str, Any] | None, *, override: bool | None
) -> bool:
    """The regulatory-significance axis flag for a request (domains.md §4(3)).

    A request is regulatorily-significant if an explicit ``override`` says so, or
    the request view marks it (``regulatory_significant`` / ``regulatory_significance``
    truthy). This is the flag that forces the strict path independent of diff size
    (a one-line APR/dosing change is NOT Low).
    """
    if override is not None:
        return override
    if request is None:
        return False
    for key in ("regulatory_significant", "regulatory_significance", "regulatorily_significant"):
        if bool(request.get(key)):
            return True
    return False


def resolve_effective_mode_tier(
    policy: ModeTierPolicy,
    *,
    requested_mode: str = MODE_AUTONOMOUS,
    request: Mapping[str, Any] | None = None,
    capabilities: Iterable[str] | None = None,
    seq: Seq,
    run_against: str = "",
    regulatory_significant: bool | None = None,
) -> EffectiveModeTier:
    """Resolve the EFFECTIVE mode/tier for a request under a composed ``policy``.

    Fail-closed + tighten-only (domains.md §4(2,3), governance.md §1e):

    * **Mode veto.** ``policy.vetoed_modes`` are made UNREACHABLE. The reachable set
      is the candidate :data:`RELEASE_MODES` minus the veto. If the regulatory-
      significance axis fires, ``autonomous`` AND ``assistant`` (auto-approve) are
      additionally treated as unreachable — the run MUST be Gated with a human.
      The effective mode is the MOST-permissive still-reachable mode that is no
      more permissive than ``requested_mode`` (a vetoed requested mode falls back
      to the strictest allowed — never silently keeps the vetoed mode).
    * **Tier raise.** The base tier is the deterministic
      :func:`~foundry.governance.tiering.classify_tier` verdict; the effective tier
      is the STRICTER of it and ``policy.min_tier`` (the veto can only RAISE it). A
      regulatory-significant request floors the tier at HIGH.
    * **Forced human / two-person.** Forced-on by ``policy.force_human_release`` /
      ``policy.two_person_release`` OR by a regulatory-significant request — a named
      human accountable approver is mandatory; auto-approve cannot override.

    Pure/deterministic: no I/O — the verdict is a function of the policy + request.
    """
    reg_sig = _is_regulatory_significant(request, override=regulatory_significant)

    # -- base tier from the deterministic classifier (reused, not rebuilt) ------
    source: str | Mapping[str, Any] = request if request is not None else ""
    base = classify_tier(source, capabilities=capabilities, seq=seq, run_against=run_against)
    classifier_tier = base.tier

    # -- effective tier: STRICTER of (classifier, policy.min_tier); reg-sig=HIGH -
    effective_tier = _stricter_tier(classifier_tier, policy.min_tier)
    if reg_sig:
        effective_tier = _stricter_tier(effective_tier, RiskTier.HIGH)

    # -- reachable modes: candidates minus the veto (+ reg-sig forces Gated) -----
    reachable = set(policy.allowed_modes(RELEASE_MODES))
    if reg_sig:
        # The strict path: hands-off Autonomous + auto-approve Assistant are
        # UNREACHABLE — a regulatorily-significant change must be Gated with a
        # recorded human/approver (Regulated-F1/F5; never auto-approved).
        reachable.discard(MODE_AUTONOMOUS)
        reachable.discard(MODE_ASSISTANT)
    if not reachable:
        # Fail-closed: a fully-vetoed candidate set degrades to the STRICTEST mode
        # (Gated), never to "anything goes". The veto can only tighten.
        reachable = {MODE_GATED}

    effective_mode = _resolve_mode(requested_mode, reachable)
    autonomous_reachable = MODE_AUTONOMOUS in reachable

    force_human = policy.force_human_release or reg_sig or not autonomous_reachable
    two_person = policy.two_person_release or reg_sig

    return EffectiveModeTier(
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        reachable_modes=frozenset(reachable),
        effective_tier=effective_tier,
        classifier_tier=classifier_tier,
        force_human_release=force_human,
        two_person_release=two_person,
        regulatory_significant=reg_sig,
        autonomous_reachable=autonomous_reachable,
    )


def _stricter_tier(a: RiskTier, b: RiskTier) -> RiskTier:
    """The STRICTER (higher-strictness) of two risk tiers (tighten-only)."""
    return a if _TIER_STRICTNESS[a] >= _TIER_STRICTNESS[b] else b


def _resolve_mode(requested_mode: str, reachable: set[str]) -> str:
    """The effective mode: the most-permissive reachable mode no more permissive
    than ``requested_mode`` (fail-closed: a vetoed requested mode falls back to the
    strictest still-reachable mode — NEVER keeps the vetoed mode)."""
    req = requested_mode.lower()
    req_rank = _MODE_PERMISSIVENESS.get(req)
    # Candidates no more permissive than what was requested (so a Gated request
    # can never be widened to Autonomous by the resolver — tighten-only).
    if req_rank is not None:
        capped = {m for m in reachable if _MODE_PERMISSIVENESS[m] <= req_rank}
    else:
        capped = set(reachable)
    pool = capped or reachable  # if nothing at/below the request, take the strictest below
    # The most-permissive still-allowed mode in the pool (deterministic).
    return max(pool, key=lambda m: _MODE_PERMISSIVENESS[m])


# --- (e) per-artifact approver-of-record -------------------------------------


class ApproverRequirement(FrozenModel):
    """Whether a request's artifact CLASS requires a class-4 approver-of-record.

    ``required`` is set iff the artifact class is in the policy's bound set; when
    required, an Autonomous auto-approve is UNREACHABLE — only a recorded human
    sign-off can satisfy it (Regulated-F2: agent-identity class-4 writes rejected).
    """

    artifact_class: str
    required: bool
    bound_classes: frozenset[str]


def approver_required(policy: ApproverPolicy, *, artifact_class: str) -> ApproverRequirement:
    """Does ``artifact_class`` require a class-4 approver-of-record under ``policy``?

    Pure: ``required`` iff the class is in ``policy.bound_artifact_classes`` (e.g.
    design / test_protocol / risk_analysis / validation_report — domains.md §4(4)).
    """
    bound = frozenset(c.lower() for c in policy.bound_artifact_classes)
    return ApproverRequirement(
        artifact_class=artifact_class,
        required=artifact_class.lower() in bound,
        bound_classes=bound,
    )


def _is_human_approver(approver: str) -> bool:
    """A class-4 approver-of-record must be a NAMED HUMAN — never the autonomous
    auto-approver / agent / self-judge sentinel (Regulated-F2, fail-closed)."""
    return approver.strip().lower() not in _NON_HUMAN_APPROVERS


def bind_approver_of_record(
    policy: ApproverPolicy,
    *,
    artifact_class: str,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
    approver: str,
    role: str,
    profile: str,
    seq: Seq,
    second_approver: str | None = None,
    second_role: str | None = None,
    second_nonce: str | None = None,
    second_seq: Seq | None = None,
    meaning_of_signing: str = "",
    liaison: HumanLiaison | None = None,
) -> tuple[SignOffRecord, ...]:
    """Bind a NAMED human approver-of-record to a class-4 artifact (Regulated-F2).

    Reuses :func:`foundry.governance.sign_off.record_sign_off` — the approver is
    RECORDED (the always-recorded invariant); two-person where required (a second
    distinct approver/role/nonce is recorded too). Fail-closed:

    * The artifact class MUST be in ``policy.bound_artifact_classes`` — binding an
      approver to an unbound class is a programming error (raises ``SignOffError``).
    * ``approver`` MUST be a recorded human ≠ the autonomous/agent auto-approver
      sentinel — an agent-identity / Autonomous auto-approve can NEVER satisfy a
      class-4 binding (raises ``SignOffError``; no human approver ⇒ no release).
    * For two-person, the second approver MUST be a distinct human identity in a
      distinct role (segregation of duties, Regulated-F3a).

    Returns the recorded sign-off(s) (one, or two for two-person). The downstream
    release admission (:func:`release_admitted`) re-checks the binding fail-closed.
    """
    if not approver_required(policy, artifact_class=artifact_class).required:
        raise SignOffError(
            f"approver-of-record binding refused: artifact class {artifact_class!r} is not "
            f"in the pack's bound class-4 set {sorted(policy.bound_artifact_classes)!r} "
            "(domains.md §4(4))"
        )
    if not _is_human_approver(approver):
        raise SignOffError(
            f"class-4 approver-of-record must be a NAMED HUMAN, not {approver!r} — an "
            "agent-identity / Autonomous auto-approve is rejected (Regulated-F2, fail-closed)"
        )
    primary = record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
        approver=approver,
        role=role,
        profile=profile,
        seq=seq,
        meaning_of_signing=meaning_of_signing or f"approver-of-record: {artifact_class}",
        liaison=liaison,
    )
    records: tuple[SignOffRecord, ...] = (primary,)
    if second_approver is not None:
        if (
            not _is_human_approver(second_approver)
            or second_approver.strip().lower() == approver.strip().lower()
            or (second_role or "").strip().lower() == role.strip().lower()
        ):
            raise SignOffError(
                "two-person approver-of-record requires a SECOND distinct human in a "
                "DISTINCT role (segregation of duties, Regulated-F3a) — got "
                f"{second_approver!r}/{second_role!r} vs {approver!r}/{role!r}"
            )
        records = (
            *records,
            record_sign_off(
                SignOffPoint.RELEASE,
                subject_artifact_uri=subject_artifact_uri,
                state_sha=state_sha,
                presented_evidence_hash=presented_evidence_hash,
                server_nonce=second_nonce or server_nonce,
                approver=second_approver,
                role=second_role or role,
                profile=profile,
                seq=second_seq or seq,
                meaning_of_signing=(
                    meaning_of_signing or f"approver-of-record (2nd): {artifact_class}"
                ),
                liaison=liaison,
            )
        )
    return records


def release_admitted(
    policy: ApproverPolicy,
    records: Iterable[SignOffRecord] | None,
    *,
    artifact_class: str,
    subject_artifact_uri: str,
    state_sha: str,
    require_two_person: bool = False,
) -> bool:
    """Fail-closed release admission for an approver-of-record-bound artifact.

    For a class in ``policy.bound_artifact_classes`` a release is admitted ONLY if a
    recorded, ``approved``, HUMAN sign-off binds THIS artifact + ``state_sha``
    (Regulated-F2; no approver ⇒ no release). When ``require_two_person`` is set,
    TWO distinct human approvers in DISTINCT roles are required.

    A class NOT in the bound set needs no approver-of-record (returns ``True`` — the
    other governance hooks still apply; this hook is a no-op for that class).
    """
    if not approver_required(policy, artifact_class=artifact_class).required:
        return True
    pool = [
        r
        for r in (records or ())
        if r.approved
        and r.subject_artifact_uri == subject_artifact_uri
        and r.state_sha == state_sha
        and _is_human_approver(r.approver)
    ]
    if not pool:
        return False  # no recorded human approver ⇒ no release (fail-closed)
    if require_two_person:
        identities = {r.approver.strip().lower() for r in pool}
        roles = {r.role.strip().lower() for r in pool}
        return len(identities) >= 2 and len(roles) >= 2
    return True


# --- (f) data-egress profile -------------------------------------------------


class EgressRequest(FrozenModel):
    """An advisory view of a request's egress posture, checked against a profile.

    Each flag asserts the request WOULD perform that egress (a model/trace call to
    a third party, or real PHI/PAN entering the pipeline). The profile FORBIDS the
    corresponding axis; a forbidden-but-present axis is a fail-closed violation.
    """

    third_party_model_egress: bool = False
    third_party_trace_egress: bool = False
    real_phi_pan: bool = False


class EgressVerdict(FrozenModel):
    """The deny/allow verdict of an egress check against a composed profile."""

    allowed: bool
    violations: tuple[str, ...] = ()


def egress_within_profile(policy: EgressPolicy, egress: EgressRequest) -> EgressVerdict:
    """Is ``egress`` WITHIN the pack's ``policy`` (domains.md §4(5), Regulated-F4)?

    Fail-closed deny on ANY violation: a request performing an egress the profile
    FORBIDS (no third-party model/trace egress; no real PHI/PAN in the pipeline) is
    denied, with the violated axes enumerated. Pure/deterministic — no I/O.
    """
    violations: list[str] = []
    if policy.no_third_party_model_egress and egress.third_party_model_egress:
        violations.append("third_party_model_egress")
    if policy.no_third_party_trace_egress and egress.third_party_trace_egress:
        violations.append("third_party_trace_egress")
    if policy.no_real_phi_pan and egress.real_phi_pan:
        violations.append("real_phi_pan")
    return EgressVerdict(allowed=not violations, violations=tuple(violations))


def assert_egress_within_profile(policy: EgressPolicy, egress: EgressRequest) -> None:
    """Like :func:`egress_within_profile` but RAISES :class:`GovernanceHookError`
    on a violation (fail-closed for the path that must refuse, not branch)."""
    verdict = egress_within_profile(policy, egress)
    if not verdict.allowed:
        raise GovernanceHookError(
            "data-egress profile violated (fail-closed deny, Regulated-F4): "
            f"{list(verdict.violations)} forbidden by the engaged pack's egress profile"
        )


# --- (g) mandatory perceptual HITL + rendered evidence -----------------------


class PerceptualHITLRequirement(FrozenModel):
    """Whether a request requires a non-auto-approvable perceptual/UX HITL.

    ``required`` is set iff the request touches a capability in the policy's
    ``hitl_required_capabilities`` (e.g. ``ui``). When required, the change MUST
    pass a visual HITL over a rendered-evidence bundle and can NEVER be auto-
    approved (UX-F1/F2; class-5 — excluded from the signed predicate but gated).
    """

    required: bool
    matched_capabilities: frozenset[str]
    require_rendered_evidence: bool
    auto_approvable: bool


def perceptual_hitl_required(
    policy: PerceptualHITLPolicy,
    *,
    request: Mapping[str, Any] | None = None,
    capabilities: Iterable[str] | None = None,
) -> PerceptualHITLRequirement:
    """Does this request require a mandatory perceptual HITL (domains.md §4(6))?

    Reads the request's capability set (the ``required_capabilities`` view, mirrored
    by :func:`foundry.domains.contract.request_capabilities`) plus any explicit
    ``capabilities=`` and intersects it with ``policy.hitl_required_capabilities``.
    A non-empty intersection ⇒ a mandatory, NON-auto-approvable visual HITL over a
    rendered-evidence bundle (``auto_approvable`` is always ``False`` when required).
    Pure/deterministic.
    """
    caps: set[str] = set()
    if request is not None:
        caps |= request_capabilities(request)
    if capabilities is not None:
        caps |= {str(c).lower() for c in capabilities}
    required_caps = frozenset(c.lower() for c in policy.hitl_required_capabilities)
    matched = frozenset(caps & required_caps)
    required = bool(matched)
    return PerceptualHITLRequirement(
        required=required,
        matched_capabilities=matched,
        # The rendered evidence bundle is mandatory whenever HITL is required (the
        # class-5 verdict is grounded over a rendered bundle), OR the policy pins it.
        require_rendered_evidence=required or policy.require_rendered_evidence,
        # A perceptual-HITL-required change is NEVER auto-approvable (UX-F1/F2).
        auto_approvable=not required,
    )


def mark_perceptual_hitl(
    policy: PerceptualHITLPolicy,
    *,
    request: Mapping[str, Any] | None = None,
    capabilities: Iterable[str] | None = None,
    human_sign_off: SignOffRecord | None = None,
) -> SignOffRecord | None:
    """Mark a perceptual-HITL-required request — fail-closed, never auto-approved.

    If the request requires a mandatory perceptual HITL (:func:`perceptual_hitl_required`)
    the ONLY resolution is a RECORDED, ``approved`` HUMAN sign-off over the rendered-
    evidence bundle. Passing no (or an un-approved) ``human_sign_off`` for a required
    change raises :class:`SignOffError` (the gate can never be cleared automatically —
    UX-F1/F2: never auto-approved). Returns the human sign-off when present + approved;
    returns ``None`` when no HITL is required (the hook is a no-op for that request).
    """
    req = perceptual_hitl_required(policy, request=request, capabilities=capabilities)
    if not req.required:
        return None
    if human_sign_off is None or not human_sign_off.approved:
        caps = sorted(req.matched_capabilities)
        raise SignOffError(
            "mandatory perceptual HITL blocked (UX-F1/F2, fail-closed): a class-5 visual "
            f"HITL over a rendered-evidence bundle for capabilities {caps} requires a "
            "RECORDED, approved human sign-off — it is NEVER auto-approved"
        )
    return human_sign_off
