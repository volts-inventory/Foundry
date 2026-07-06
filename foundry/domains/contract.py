"""The `DomainPackContract` — the extension seam for domain packs. (domains.md §4)

A **domain pack** teaches the general factory how a domain's *correctness* is
shaped (regulated / ML / frontend) WITHOUT editing core. It does so by overriding
a small set of typed **hooks**; the core calls every engaged pack's hook and
**composes** the results strictest-wins (see :mod:`foundry.domains.registry`).

The load-bearing invariant (domains.md §4, governance.md §1e/§5): a pack can only
**TIGHTEN** — make policy *stricter* (force Gated, add a gate, bind an approver,
force-escalate the tier, veto a mode, add a ``state_sha`` input, demand HITL). A
pack can **NEVER relax** admission/integrity. This is not a convention: every hook
return type is a *monotone* policy object whose ``tighten(other)`` join takes the
STRICTER side on every axis, and whose ``relaxes(base)`` predicate the registry
asserts is ``False`` during composition (``tighten_only``). A hypothetical
"relaxing" override therefore has **no effect on the integrity floor** — the join
keeps the stricter value, and the registry's ``tighten_only`` check additionally
REJECTS a pack whose policy would lower an already-established floor.

Each hook has a **SAFE DEFAULT** (the no-op identity on :class:`BaseDomainPack`),
so a concrete pack overrides only the hooks it cares about. A pack that overrides
nothing engages nothing — it is the identity element of the composition.

The seven hooks (domains.md §4):

1. :meth:`assign_verdict_class`  — verdict-class assignment (classes 1-5).
2. :meth:`state_inputs`          — extra ``state_sha`` components (data/weight refs).
3. :meth:`compute_budget`        — compute-cost budget + expensive-gate handling.
4. :meth:`mode_tier_policy`      — mode/tier veto + regulatory-significance axis.
5. :meth:`approver_of_record`    — per-artifact class-4 human attestation binding.
6. :meth:`egress_profile`        — data-egress profile (no third-party egress, no PHI).
7. :meth:`perceptual_hitl`       — mandatory perceptual HITL + rendered evidence.

Every policy below is a frozen pydantic model (L3) with two structural methods:

* ``tighten(other) -> Self`` — the strictest-wins JOIN (commutative, associative,
  idempotent; the registry folds engaged packs through it).
* ``relaxes(floor) -> bool`` — would adopting ``self`` LOWER the integrity floor
  ``floor``? The registry asserts this is ``False`` (``tighten_only``).

``identity()`` is the neutral element: ``X.tighten(identity()) == X`` and
``identity().relaxes(anything) is False``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import Field

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import VerdictClass
from foundry.schemas.governance import RiskTier

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Any

__all__ = [
    "AIJudgePolicy",
    "ApproverPolicy",
    "BaseDomainPack",
    "ComputePolicy",
    "DomainPackContract",
    "EgressPolicy",
    "ModeTierPolicy",
    "PerceptualHITLPolicy",
    "StateInputPolicy",
    "TightenError",
    "VerdictClassPolicy",
    "request_capabilities",
    "request_is_autonomous",
]


class TightenError(RuntimeError):
    """A pack attempted to RELAX an already-established integrity floor.

    Raised by the registry's ``tighten_only`` composition when a pack's policy
    would lower the strictness of the running floor — the seam refuses it
    fail-closed (domains.md §4: a pack can only tighten, never relax).
    """


# Strictness order over the modes the factory can run a release in. A pack VETOES
# the more permissive modes by raising the floor: Assistant < Gated < Autonomous
# in *permissiveness* of the release boundary, so a regulated pack forcing a
# human/recorded sign-off vetoes BOTH Autonomous and (auto-approve) Assistant by
# pinning the floor at GATED. "Strictest" = the LEAST permissive still allowed.
_MODE_PERMISSIVENESS: dict[str, int] = {
    "autonomous": 2,  # most permissive: trust-root auto-release, no human
    "assistant": 1,  # auto-approve-able Phase-1 flow
    "gated": 0,  # least permissive: recorded human/release sign-off required
}

# Strictness order over risk tiers (governance.md §1e). HIGH is the strictest.
_TIER_STRICTNESS: dict[RiskTier, int] = {
    RiskTier.LOW: 0,
    RiskTier.STANDARD: 1,
    RiskTier.HIGH: 2,
}

# Strictness order over verdict classes (domains.md §1). A pack may REROUTE a
# criterion to a STRICTER grounding (e.g. perceptual/human-witnessed instead of a
# self-judge residual) but may never downgrade the grounding of an integrity
# verdict. Higher rank = harder to forge / more human-bound.
# NB (M5-R1-02): PERCEPTUAL (class 5) is ranked BELOW HUMAN_WITNESSED (class 4) ON
# PURPOSE — this is NOT the literal class-5 > class-4 numbering. For *satisfaction*
# (``tighten`` keeps the higher rank; ``satisfies(have, need)`` is True iff
# ``rank[have] >= rank[need]``), a recorded HUMAN attestation (HUMAN_WITNESSED) is at
# least as strong as a perceptual HITL: a named human signed, both are
# non-auto-approvable, and neither loses integrity. So a HUMAN_WITNESSED sign-off
# satisfies a key requiring PERCEPTUAL grounding. Do NOT "fix" this into
# class-5 > class-4 — that would REGRESS the satisfaction lattice (a present human
# attestation would stop satisfying a perceptual-required key). This ranking only
# governs the satisfaction/tighten JOIN; it does NOT make either class
# auto-approvable — class 4 AND class 5 both remain never-auto-approvable (enforced
# by ``verdict_class`` / ``perceptual_hitl_required``, which this rank never relaxes).
_VERDICT_CLASS_STRICTNESS: dict[VerdictClass, int] = {
    VerdictClass.CONFIG_EVALUABLE: 0,
    VerdictClass.EXECUTION_DETERMINISTIC: 1,
    VerdictClass.STATISTICAL: 2,
    VerdictClass.PERCEPTUAL: 3,
    VerdictClass.HUMAN_WITNESSED: 4,  # strictest for satisfaction: a named human attested
}


class VerdictClassPolicy(FrozenModel):
    """Hook 1 — verdict-class assignment per gate/criterion (domains.md §1).

    Maps a gate/criterion key -> the verdict class it must be grounded by. The
    join keeps the STRICTER class per key (never downgrades a grounding); the
    union over keys is monotone (adding a keyed requirement only tightens).
    """

    classes: dict[str, VerdictClass] = Field(default_factory=dict)

    @classmethod
    def identity(cls) -> VerdictClassPolicy:
        return cls()

    def tighten(self, other: VerdictClassPolicy) -> VerdictClassPolicy:
        merged = dict(self.classes)
        for key, vc in other.classes.items():
            cur = merged.get(key)
            if cur is None or _VERDICT_CLASS_STRICTNESS[vc] > _VERDICT_CLASS_STRICTNESS[cur]:
                merged[key] = vc
        return VerdictClassPolicy(classes=merged)

    def relaxes(self, floor: VerdictClassPolicy) -> bool:
        """True iff adopting ``self`` would DOWNGRADE any keyed grounding in ``floor``."""
        rank = _VERDICT_CLASS_STRICTNESS
        for key, fvc in floor.classes.items():
            mine = self.classes.get(key)
            if mine is not None and rank[mine] < rank[fvc]:
                return True
        return False


class StateInputPolicy(FrozenModel):
    """Hook 2 — extra ``state_sha`` components (domains.md §2).

    Declares which extra content-identity ref sets enter ``state_sha`` so a
    data/weight/feature change dirties exactly the data-dependent gates. The join
    is a pure UNION (adding a verification input only ever tightens the key —
    more inputs can never make a stale PASS reusable).
    """

    data_refs: frozenset[str] = frozenset()
    feature_defs: frozenset[str] = frozenset()
    weight_refs: frozenset[str] = frozenset()

    @classmethod
    def identity(cls) -> StateInputPolicy:
        return cls()

    def tighten(self, other: StateInputPolicy) -> StateInputPolicy:
        return StateInputPolicy(
            data_refs=self.data_refs | other.data_refs,
            feature_defs=self.feature_defs | other.feature_defs,
            weight_refs=self.weight_refs | other.weight_refs,
        )

    def relaxes(self, floor: StateInputPolicy) -> bool:
        """Always ``False`` — the join is a pure UNION, which can never narrow the
        ``state_sha`` key. A pack contributing only its own ref delta preserves
        every floor input through ``tighten`` (more verification inputs only ever
        tighten the key — a stale PASS can never be re-laundered). (domains.md §2)"""
        del floor
        return False


class ComputePolicy(FrozenModel):
    """Hook 3 — compute-cost budget + expensive-gate handling (domains.md §3).

    ``expensive_gates`` are gates (a retrain, a device-matrix visual run) that
    must run behind a cheap proxy + an explicit confirm-budget escalation and
    must NOT be auto-re-run on every fix. The join unions the expensive-gate set
    and keeps the LOWER per-fix compute cap when both declare one (the tighter
    budget wins; ``None`` = unset, dominated by any concrete cap).
    """

    expensive_gates: frozenset[str] = frozenset()
    require_proxy: bool = False  # expensive gates gated behind a cheap proxy
    confirm_escalation: bool = False  # full run requires explicit budget confirm
    # Compute UNITS (NOT micro-USD) — a per-fix ceiling metered by
    # ``state_sha.ComputeBudget`` (integer compute units; a GPU-minute / retrain
    # slice / device-matrix cell), the dimension DISTINCT from USD spend (M5-R1-03).
    per_fix_compute_cap_units: int | None = None

    @classmethod
    def identity(cls) -> ComputePolicy:
        return cls()

    def tighten(self, other: ComputePolicy) -> ComputePolicy:
        caps = [
            c
            for c in (self.per_fix_compute_cap_units, other.per_fix_compute_cap_units)
            if c is not None
        ]
        return ComputePolicy(
            expensive_gates=self.expensive_gates | other.expensive_gates,
            require_proxy=self.require_proxy or other.require_proxy,
            confirm_escalation=self.confirm_escalation or other.confirm_escalation,
            per_fix_compute_cap_units=min(caps) if caps else None,
        )

    def relaxes(self, floor: ComputePolicy) -> bool:
        """True iff ``self`` would RAISE the (displacing) per-fix compute cap above
        the floor's. The expensive-gate set + proxy/confirm flags are union/OR axes
        the join can never weaken, so they need no check; the only displacing axis
        is the cap (``tighten`` keeps the LOWER), where a contribution SPECIFYING a
        higher cap than the floor is an attempted relaxation. (domains.md §3)"""
        fc, sc = floor.per_fix_compute_cap_units, self.per_fix_compute_cap_units
        return fc is not None and sc is not None and sc > fc


class ModeTierPolicy(FrozenModel):
    """Hook 4 — mode/tier veto + regulatory-significance axis (domains.md §4(2,3)).

    * ``vetoed_modes`` — modes made UNREACHABLE (e.g. ``autonomous`` + low-tier
      auto-approve). Represented as a release-permissiveness FLOOR: the strictest
      (least-permissive) mode any engaged pack still allows wins.
    * ``min_tier`` — a regulatory-significance force-escalation of the change-risk
      tier independent of diff size (a one-line APR/dosing change is not Low).
    * ``force_human_release`` — force-on a named human accountable approver at
      Release (auto-approve cannot override; governance §1e).
    * ``two_person_release`` — distinct-identity / distinct-role sign-off.
    """

    vetoed_modes: frozenset[str] = frozenset()
    min_tier: RiskTier = RiskTier.LOW
    force_human_release: bool = False
    two_person_release: bool = False

    @classmethod
    def identity(cls) -> ModeTierPolicy:
        return cls()

    def allowed_modes(self, candidates: Iterable[str]) -> frozenset[str]:
        """The candidate modes NOT vetoed by this policy (the reachable set)."""
        vetoed = {m.lower() for m in self.vetoed_modes}
        return frozenset(m for m in candidates if m.lower() not in vetoed)

    def tighten(self, other: ModeTierPolicy) -> ModeTierPolicy:
        a, b = _TIER_STRICTNESS[self.min_tier], _TIER_STRICTNESS[other.min_tier]
        return ModeTierPolicy(
            vetoed_modes=self.vetoed_modes | other.vetoed_modes,
            min_tier=self.min_tier if a >= b else other.min_tier,
            force_human_release=self.force_human_release or other.force_human_release,
            two_person_release=self.two_person_release or other.two_person_release,
        )

    def relaxes(self, floor: ModeTierPolicy) -> bool:
        """True iff ``self`` would LOWER the floor on a DISPLACING axis.

        Union/OR axes (``vetoed_modes``, the boolean force flags) can never relax
        through the monotone join — the join keeps every vetoed mode and every set
        flag — so they need no check here. The only displacing axis is ``min_tier``
        (``tighten`` picks the stricter of two tiers): a contribution that SPECIFIES
        a strictly-lower tier than the floor is an attempted relaxation, caught
        fail-closed by the registry's ``tighten_only`` fold (domains.md §4). The
        identity tier (``LOW``) is no-opinion — it never relaxes."""
        if self.min_tier is RiskTier.LOW:  # no-opinion default — not a specification
            return False
        return _TIER_STRICTNESS[self.min_tier] < _TIER_STRICTNESS[floor.min_tier]


class ApproverPolicy(FrozenModel):
    """Hook 5 — per-artifact-class approver-of-record (domains.md §4(4), Regulated-F2).

    Declares the artifact CLASSES (design / test_protocol / risk_analysis /
    validation_report …) that require a class-4 human attestation bound into WORM
    + predicate. The join is a pure UNION (binding more artifact classes only ever
    tightens — an agent-identity class-4 write is rejected downstream regardless).
    """

    bound_artifact_classes: frozenset[str] = frozenset()

    @classmethod
    def identity(cls) -> ApproverPolicy:
        return cls()

    def tighten(self, other: ApproverPolicy) -> ApproverPolicy:
        return ApproverPolicy(
            bound_artifact_classes=self.bound_artifact_classes | other.bound_artifact_classes
        )

    def relaxes(self, floor: ApproverPolicy) -> bool:
        """Always ``False`` — the join is a pure UNION, so a pack contributing only
        its own bound artifact classes can never UNBIND a floor class; every bound
        class survives ``tighten``. (domains.md §4(4))"""
        del floor
        return False


class EgressPolicy(FrozenModel):
    """Hook 6 — data-egress profile (domains.md §4(5), Regulated-F4).

    Each flag, once set, can only stay set (a pack can forbid third-party egress
    or real PHI/PAN in the pipeline; another pack engaging cannot re-permit it).
    The join is a logical OR per flag; ``relaxes`` is dropping any set flag.
    """

    no_third_party_model_egress: bool = False
    no_third_party_trace_egress: bool = False
    no_real_phi_pan: bool = False

    @classmethod
    def identity(cls) -> EgressPolicy:
        return cls()

    def tighten(self, other: EgressPolicy) -> EgressPolicy:
        return EgressPolicy(
            no_third_party_model_egress=self.no_third_party_model_egress
            or other.no_third_party_model_egress,
            no_third_party_trace_egress=self.no_third_party_trace_egress
            or other.no_third_party_trace_egress,
            no_real_phi_pan=self.no_real_phi_pan or other.no_real_phi_pan,
        )

    def relaxes(self, floor: EgressPolicy) -> bool:
        """Always ``False`` — every axis is an OR flag the join can only set, never
        clear, so a pack passing ``False`` for a flag the floor set to ``True``
        cannot re-permit that egress; ``tighten`` keeps the forbidding flag.
        (domains.md §4(5))"""
        del floor
        return False


class PerceptualHITLPolicy(FrozenModel):
    """Hook 7 — mandatory perceptual HITL + rendered evidence (domains.md §4(6), UX-F1/F2).

    Declares the capabilities (e.g. ``ui``) whose changes REQUIRE a non-auto-
    approvable perceptual/UX HITL over a rendered-artifact evidence bundle, and
    whether a rendered evidence bundle is mandatory. The join unions the required
    capability set and ORs the mandatory-bundle flag — both monotone (demanding
    HITL on more capabilities only tightens; it is never auto-approvable).
    """

    hitl_required_capabilities: frozenset[str] = frozenset()
    require_rendered_evidence: bool = False

    @classmethod
    def identity(cls) -> PerceptualHITLPolicy:
        return cls()

    def tighten(self, other: PerceptualHITLPolicy) -> PerceptualHITLPolicy:
        return PerceptualHITLPolicy(
            hitl_required_capabilities=self.hitl_required_capabilities
            | other.hitl_required_capabilities,
            require_rendered_evidence=self.require_rendered_evidence
            or other.require_rendered_evidence,
        )

    def relaxes(self, floor: PerceptualHITLPolicy) -> bool:
        """Always ``False`` — union (required caps) + OR (evidence) axes the join can
        only grow, so a pack contributing its own HITL demand can never drop a floor
        requirement; ``tighten`` keeps every required capability + the evidence
        demand. (domains.md §4(6))"""
        del floor
        return False


class AIJudgePolicy(FrozenModel):
    """Hook 8 — AI-expert release judgment for hands-off (AUTONOMOUS) runs.

    The universal rule: in the human-in-the-loop modes a HUMAN is the judge (the
    perceptual HITL / approver-of-record hooks); in a declared-autonomous run an
    AI EXPERT fills the same judgment slot instead of it being skipped. A pack
    declares per-DOMAIN rubrics (``domain -> what the judge must evaluate``); the
    release boundary runs each rubric through the proxy's TRUSTED judgment path
    (grammar-constrained verdict, proxy-stamped binding to the exact subject +
    state_sha) and admits only if EVERY domain approves — fail-closed.

    The join unions the rubric map (judging more domains only tightens); two
    packs contributing rubrics for the SAME domain concatenate them.
    """

    rubrics: dict[str, str] = Field(default_factory=dict)

    @property
    def required(self) -> bool:
        return bool(self.rubrics)

    @classmethod
    def identity(cls) -> AIJudgePolicy:
        return cls()

    def tighten(self, other: AIJudgePolicy) -> AIJudgePolicy:
        merged = dict(self.rubrics)
        for domain, rubric in other.rubrics.items():
            prior = merged.get(domain)
            if prior and rubric and rubric not in prior:
                merged[domain] = f"{prior}\n\n{rubric}"
            else:
                merged[domain] = rubric or prior or ""
        return AIJudgePolicy(rubrics=merged)

    def relaxes(self, floor: AIJudgePolicy) -> bool:
        """Always ``False`` — a union axis: contributing a rubric can only add
        scrutiny, never drop another pack's demand (``tighten`` keeps every
        domain's rubric)."""
        del floor
        return False


@runtime_checkable
class DomainPackContract(Protocol):
    """Structural contract every pack satisfies (domains.md §4).

    A pack declares the ``capabilities`` it serves and may override any of the
    seven hooks. The contract is a ``Protocol``, so a pack need not subclass
    anything — but :class:`BaseDomainPack` supplies the safe no-op defaults so a
    pack overrides ONLY what it tightens. Hooks are PURE (no I/O, no state
    mutation): they read an advisory request view and return a monotone policy.
    """

    @property
    def name(self) -> str:
        """Stable pack identity (for audit / WORM records / dedup)."""
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """The capabilities this pack engages on (routing key, registry)."""
        ...

    # --- the eight core hooks (domains.md §4) ---------------------------------

    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        """Hook 1 — verdict-class assignment per criterion/gate (classes 1-5)."""
        ...

    def state_inputs(self, request: Mapping[str, Any]) -> StateInputPolicy:
        """Hook 2 — extra ``state_sha`` components (data/feature/weight refs)."""
        ...

    def compute_budget(self, request: Mapping[str, Any]) -> ComputePolicy:
        """Hook 3 — compute-cost budget + expensive-gate proxy/confirm."""
        ...

    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        """Hook 4 — mode/tier veto + regulatory-significance escalation axis."""
        ...

    def approver_of_record(self, request: Mapping[str, Any]) -> ApproverPolicy:
        """Hook 5 — per-artifact-class class-4 approver-of-record binding."""
        ...

    def egress_profile(self, request: Mapping[str, Any]) -> EgressPolicy:
        """Hook 6 — data-egress profile (no third-party egress / no PHI)."""
        ...

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        """Hook 7 — mandatory perceptual HITL + rendered-evidence bundle."""
        ...

    def ai_judgment(self, request: Mapping[str, Any]) -> AIJudgePolicy:
        """Hook 8 — AI-expert release judgment rubrics for AUTONOMOUS runs."""
        ...


class BaseDomainPack:
    """Safe-default base/mixin: every hook is the no-op IDENTITY of the join.

    A pack subclasses this and overrides ONLY the hooks it tightens; the rest
    return the neutral policy that, joined with anything, leaves it unchanged
    (``X.tighten(IDENTITY) == X``). This is what makes "a base pack engages
    nothing" testable, and what lets a real pack add policy without touching the
    other six axes.

    The base is intentionally NOT abstract: instantiating ``BaseDomainPack()``
    directly yields a valid identity pack (used as the composition seed and in
    tests). Concrete packs set ``name`` / ``capabilities`` (a class attr or a
    property) and override hooks.
    """

    #: Subclasses override; the bare base is the anonymous identity pack.
    name: str = "base"
    #: Capabilities this pack engages on (a pack with none never routes).
    capabilities: frozenset[str] = frozenset()

    # Each default returns the IDENTITY policy (neutral under ``.tighten``).
    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        return VerdictClassPolicy.identity()

    def state_inputs(self, request: Mapping[str, Any]) -> StateInputPolicy:
        return StateInputPolicy.identity()

    def compute_budget(self, request: Mapping[str, Any]) -> ComputePolicy:
        return ComputePolicy.identity()

    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        return ModeTierPolicy.identity()

    def approver_of_record(self, request: Mapping[str, Any]) -> ApproverPolicy:
        return ApproverPolicy.identity()

    def egress_profile(self, request: Mapping[str, Any]) -> EgressPolicy:
        return EgressPolicy.identity()

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        return PerceptualHITLPolicy.identity()

    def ai_judgment(self, request: Mapping[str, Any]) -> AIJudgePolicy:
        return AIJudgePolicy.identity()


def request_capabilities(request: Mapping[str, Any]) -> frozenset[str]:
    """Lowercased capability set advertised on a request view (helper for packs).

    Mirrors ``governance.tiering._capabilities_of`` for the ``required_capabilities``
    key, so a pack's hook and the tiering classifier read the SAME signal.
    """
    raw: Iterable[Any] = request.get("required_capabilities") or ()
    return frozenset(str(c).lower() for c in raw)


def request_is_autonomous(request: Mapping[str, Any]) -> bool:
    """True iff the request view carries an AUTONOMOUS run mode (helper for packs
    whose human-in-the-loop requirements only bind when a human is in the loop).

    Views without a ``mode`` key (e.g. the CLI's capability-only pre-check) read
    as NOT autonomous — the strict interpretation, so a pack conditioning on this
    can only relax for a run that explicitly declared hands-off mode.
    """
    return str(request.get("mode") or "").lower() == "autonomous"
