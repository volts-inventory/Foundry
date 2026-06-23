"""Deployment-trust modes — earned autonomy, evidence-gated. (lifecycle.md §4)

Adoption does NOT start at "AI autonomously signs and deploys to prod". There
are three postures, in increasing trust:

* **Assistant** — Foundry produces a PR; a human merges via normal CI. No
  trust-root machinery.
* **Gated** — Foundry produces a release candidate behind the full gate set; a
  human approves Design + Release. Gates + evidence pack.
* **Autonomous** — Foundry builds, attests, and deploys hands-off. Requires the
  FULL trust root green (two-zone runner, SLSA-L3, admission, deployer).

A product GRADUATES ASSISTANT -> GATED -> AUTONOMOUS one step at a time, and
ONLY when the :class:`GraduationEvidence` justifies it (fail-closed: no graduation
without the evidence the target mode demands). The mode names mirror the
supervisor constants (:data:`foundry.graph.supervisor.MODE_ASSISTANT` /
``MODE_GATED`` / ``MODE_AUTONOMOUS``) — this module is the *lifecycle posture*
over the same vocabulary, not a parallel mode system.

Determinism (L7): :func:`can_graduate` / :func:`graduate` are pure functions of
the current mode + the evidence (no clock, no network, no agent code) — equal
inputs yield an equal verdict.
"""

from __future__ import annotations

from foundry.graph.supervisor import MODE_ASSISTANT, MODE_AUTONOMOUS, MODE_GATED
from foundry.schemas.base import FrozenModel

__all__ = [
    "MODE_ASSISTANT",
    "MODE_AUTONOMOUS",
    "MODE_GATED",
    "DeploymentTrustMode",
    "GraduationEvidence",
    "GraduationRefused",
    "can_graduate",
    "graduate",
]

# The graduation ladder, in increasing trust. Mirrors the supervisor constants;
# graduation moves ONE rung at a time (no skipping a posture).
_LADDER: tuple[str, ...] = (MODE_ASSISTANT, MODE_GATED, MODE_AUTONOMOUS)


class GraduationRefused(RuntimeError):
    """A mode graduation was attempted without justifying evidence — fail-closed.

    Raised by :func:`graduate` (the path that must refuse, not branch): a product
    does not graduate to a higher-trust mode unless the evidence the target mode
    demands is present (lifecycle.md §4).
    """


class GraduationEvidence(FrozenModel):
    """The evidence justifying a graduation to a higher-trust mode (§4).

    The bits are what each target posture demands:

    * GATED requires the full gate set green over the candidate
      (``gates_green``) and a recorded human design/release approval
      (``human_approval_recorded``) — Gated is "behind the full gate set, human
      approves".
    * AUTONOMOUS requires everything Gated does PLUS the FULL trust root green
      (``trust_root_green``: two-zone runner + SLSA-L3 attestation + admission +
      deployer) AND a named accountable party (``accountable_party``) — the
      Release/Admission human sign-off is the accountability anchor for hands-off
      deploy (lifecycle.md §5).

    Fail-closed by construction: every justifying bit defaults to the WEAKEST
    value (False / None), so an empty evidence object justifies NO graduation.
    """

    gates_green: bool = False  # the full gate set passed over the candidate
    human_approval_recorded: bool = False  # a recorded design/release approval
    trust_root_green: bool = False  # two-zone runner + SLSA-L3 + admission + deployer
    accountable_party: str | None = None  # the named human accountability anchor


class DeploymentTrustMode(FrozenModel):
    """A product's current deployment-trust posture (§4).

    ``mode`` is one of the supervisor constants. The product graduates by
    producing a NEW :class:`DeploymentTrustMode` (frozen — superseded, never
    mutated) via :func:`graduate`; ``prior_mode`` records the rung it came from
    so the graduation lineage is queryable.
    """

    mode: str  # MODE_ASSISTANT | MODE_GATED | MODE_AUTONOMOUS
    prior_mode: str | None = None  # the rung graduated FROM (lineage)


def _rung(mode: str) -> int:
    """The ladder index of a mode; -1 if unknown (treated as below the floor)."""
    try:
        return _LADDER.index(mode)
    except ValueError:
        return -1


def _evidence_justifies(target: str, evidence: GraduationEvidence) -> bool:
    """True iff ``evidence`` justifies operating at ``target`` (fail-closed).

    GATED: gates green + a recorded human approval.
    AUTONOMOUS: the Gated bits PLUS trust-root green + a named accountable party.
    ASSISTANT: the floor — always justified (no machinery beyond normal CI).
    """
    if target == MODE_ASSISTANT:
        return True
    if target == MODE_GATED:
        return evidence.gates_green and evidence.human_approval_recorded
    if target == MODE_AUTONOMOUS:
        return (
            evidence.gates_green
            and evidence.human_approval_recorded
            and evidence.trust_root_green
            and evidence.accountable_party is not None
        )
    return False  # unknown target — fail-closed


def can_graduate(current: DeploymentTrustMode, evidence: GraduationEvidence) -> bool:
    """True iff the product may graduate ONE rung up from ``current`` (§4).

    Fail-closed graduation predicate:

    * the current mode must be a known rung BELOW the top (you can only graduate
      UP, one step — ASSISTANT->GATED or GATED->AUTONOMOUS, never skipping);
    * the evidence must justify the NEXT rung (GATED needs gates-green + human
      approval; AUTONOMOUS additionally needs the trust root green + a named
      accountable party).

    No evidence (an empty :class:`GraduationEvidence`) ⇒ False — a product never
    graduates without the evidence the target posture demands.
    """
    cur = _rung(current.mode)
    if cur < 0 or cur >= len(_LADDER) - 1:
        return False  # unknown rung, or already at the top — nowhere to graduate
    target = _LADDER[cur + 1]
    return _evidence_justifies(target, evidence)


def graduate(
    current: DeploymentTrustMode,
    evidence: GraduationEvidence,
    *,
    strict: bool = True,
) -> DeploymentTrustMode:
    """Graduate ONE rung up if the evidence justifies it — else fail-closed (§4).

    Returns a new :class:`DeploymentTrustMode` at the next rung with
    ``prior_mode`` set to the current mode. If the evidence does NOT justify the
    graduation, ``strict=True`` (the default) raises :class:`GraduationRefused`;
    ``strict=False`` returns ``current`` UNCHANGED (a no-op — the product stays
    where it was). Either way no un-earned graduation ever happens.
    """
    if not can_graduate(current, evidence):
        if strict:
            raise GraduationRefused(
                f"product at {current.mode!r} cannot graduate — the evidence does "
                f"not justify the next trust posture (fail-closed; lifecycle.md §4)"
            )
        return current
    target = _LADDER[_rung(current.mode) + 1]
    return DeploymentTrustMode(mode=target, prior_mode=current.mode)
