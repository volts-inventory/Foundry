"""The five-plane guardrail model — explicit + inspectable. (governance §2)

Governance §2 defines five guardrail planes. The build wires them across the
codebase; this module makes that wiring **inspectable**: an operator/auditor can
enumerate all five planes and, per plane, the modules/controls that implement
it — so "are all five planes wired?" is an assertion, not a hope.

The five planes (governance §2, implementation-plan §5.3):

- **Policy** — OPA-style authorization + the run-immutable scanner config
  (this package). Deny is fail-closed.
- **Execution** — sandboxes; credential/untrusted-code separation
  (``foundry.execution.sandbox``).
- **Data** — scoped short-lived creds, egress redaction; single egress through
  the model-provider proxy (``foundry.proxy``).
- **Observability** — OTel + serving-model fingerprints + drift alerts
  (``foundry.observability`` / the proxy's manifest + pinned-ID-drift alert).
- **Assurance** — SBOM, hermetic build, SLSA, admission; the two-zone gate
  runner + durability/WORM (``foundry.gates`` + ``foundry.durability`` +
  ``foundry.trust_root``).

Plus **segregation of duties** (governance §1e) — the Policy plane's
``authorization`` control: producing and signing/admitting must not be the same
actor/zone.

The Policy plane also EVALUATES run-immutable policy: an OPA-style rule-based
evaluator over a subject. Deny rules are **fail-closed** — an explicit deny, an
unknown/missing policy, or an evaluator error all DENY (a subject is never
allowed by absence of a rule).

This is a dev-grade in-process evaluator (implementation-plan §0.3): production
swaps it for OPA/Conftest/Checkov as an ops change; the SEAM — fail-closed deny
over an inspectable plane registry — is what is real here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from foundry.schemas.base import FrozenModel

from .authorization import (
    AuthorizationDecision,
    Decision,
    DutyRequest,
    authorize_segregation_of_duties,
)

__all__ = [
    "DENY_RULES",
    "FIVE_PLANE_MODEL",
    "DenyRule",
    "FivePlaneModel",
    "Plane",
    "PlaneSpec",
    "PolicyDecision",
    "PolicyEvaluator",
    "authorize_segregation_of_duties",
]


class Plane(StrEnum):
    """The five guardrail planes. (governance §2)"""

    POLICY = "policy"
    EXECUTION = "execution"
    DATA = "data"
    OBSERVABILITY = "observability"
    ASSURANCE = "assurance"


class PlaneSpec(FrozenModel):
    """One plane's inspectable wiring: its controls + the modules that implement
    them. ``controls`` names the guardrails; ``modules`` are the import paths an
    auditor can point at to verify the plane is actually wired (not a stub)."""

    plane: Plane
    summary: str
    controls: tuple[str, ...]
    modules: tuple[str, ...]


class FivePlaneModel(FrozenModel):
    """The explicit registry mapping each :class:`Plane` to its :class:`PlaneSpec`.

    Completeness is assertable: :meth:`is_complete` is true iff every member of
    :class:`Plane` has a spec with at least one control and one implementing
    module (an empty plane is a missing guardrail, not a wired one)."""

    specs: tuple[PlaneSpec, ...]

    def by_plane(self) -> Mapping[Plane, PlaneSpec]:
        return {s.plane: s for s in self.specs}

    def planes(self) -> frozenset[Plane]:
        return frozenset(s.plane for s in self.specs)

    def is_complete(self) -> bool:
        """All five planes present, each with ≥1 control and ≥1 module."""
        mapping = self.by_plane()
        if mapping.keys() != set(Plane):
            return False
        return all(s.controls and s.modules for s in self.specs)

    def missing_planes(self) -> frozenset[Plane]:
        return frozenset(Plane) - self.planes()


#: The canonical, inspectable five-plane wiring for this build (governance §2).
FIVE_PLANE_MODEL = FivePlaneModel(
    specs=(
        PlaneSpec(
            plane=Plane.POLICY,
            summary="OPA-style authorization over tool calls + artifact outputs; "
            "run-immutable scanner config; segregation of duties.",
            controls=(
                "opa-style-deny-evaluation",
                "run-immutable-scanner-config",
                "segregation-of-duties",
            ),
            modules=(
                "foundry.policy.planes",
                "foundry.policy.scanner_config",
                "foundry.policy.authorization",
            ),
        ),
        PlaneSpec(
            plane=Plane.EXECUTION,
            summary="All agent code runs in sandboxes; credentialed phase "
            "separated from untrusted-code execution.",
            controls=(
                "worktree-only-writes",
                "credential-scrubbed-env",
                "hard-kill-cpu-walltime",
            ),
            modules=("foundry.execution.sandbox",),
        ),
        PlaneSpec(
            plane=Plane.DATA,
            summary="Scoped short-lived creds never in prompt/state; single "
            "egress through the model-provider proxy; egress redaction.",
            controls=(
                "single-egress-proxy",
                "scoped-credentials",
                "egress-redaction",
            ),
            modules=("foundry.proxy.model_provider_proxy",),
        ),
        PlaneSpec(
            plane=Plane.OBSERVABILITY,
            summary="OTel over every superstep; serving-model fingerprint "
            "observation; pinned-ID-drift alerts.",
            controls=(
                "otel-superstep-tracing",
                "served-model-fingerprint",
                "drift-alerts",
            ),
            modules=("foundry.observability", "foundry.proxy.model_provider_proxy"),
        ),
        PlaneSpec(
            plane=Plane.ASSURANCE,
            summary="Two-zone runner-attested verdicts; SBOM; hermetic build; "
            "SLSA provenance binding gate-state; fail-closed admission; WORM audit.",
            controls=(
                "two-zone-attested-verdicts",
                "sbom-slsa-provenance",
                "fail-closed-admission",
                "worm-audit",
            ),
            modules=(
                "foundry.gates.runner",
                "foundry.gates.attestation",
                "foundry.durability.outbox",
                "foundry.trust_root.admission",
            ),
        ),
    )
)


class PolicyDecision(FrozenModel):
    """A fail-closed policy decision over a subject. ``allow`` only when an
    explicit, evaluated rule permits it; otherwise ``deny`` with the reason."""

    decision: Decision
    reason: str
    matched_rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass(frozen=True)
class DenyRule:
    """An OPA-style deny rule: a named predicate over a subject mapping.

    The rule's ``predicate`` returns True when the subject is DENIED. Rules are
    deny-only (OPA default-deny shape): a subject is allowed solely when NO deny
    rule fires AND the policy is known — never by the mere absence of a rule.

    A frozen dataclass (not a ``FrozenModel``): it carries a non-serializable
    ``Callable``, so it is not a content-addressed artifact — it is pinned,
    run-immutable policy code, not run state.
    """

    name: str
    predicate: Callable[[Mapping[str, object]], bool]
    description: str = ""


def _deny_unknown_policy(subject: Mapping[str, object]) -> bool:
    # A subject with no declared policy id is unknown → fail-closed deny.
    return not subject.get("policy")


def _deny_widens_egress(subject: Mapping[str, object]) -> bool:
    # IaC/policy outputs widening egress beyond baseline require HITL → deny here.
    return bool(subject.get("widens_egress"))


def _deny_writes_scanner_config(subject: Mapping[str, object]) -> bool:
    # Agents cannot write the run-immutable scanner config (governance §2).
    return bool(subject.get("writes_scanner_config"))


def _deny_signing_from_zone_a(subject: Mapping[str, object]) -> bool:
    # Only trusted infra (zone B) reaches signing — never the producer zone.
    return subject.get("action") == "sign" and subject.get("zone") != "zone-b-attestor"


#: The pinned, run-immutable deny-rule set (OPA-style, default-deny).
DENY_RULES: tuple[DenyRule, ...] = (
    DenyRule(
        name="unknown-policy",
        description="Subject declares no known policy id (fail-closed).",
        predicate=_deny_unknown_policy,
    ),
    DenyRule(
        name="egress-widening",
        description="Output widens egress beyond the declared baseline (HITL-required).",
        predicate=_deny_widens_egress,
    ),
    DenyRule(
        name="scanner-config-write",
        description="Agent attempts to write the run-immutable scanner config.",
        predicate=_deny_writes_scanner_config,
    ),
    DenyRule(
        name="signing-outside-zone-b",
        description="Signing attempted from a non-attestor zone.",
        predicate=_deny_signing_from_zone_a,
    ),
)


class PolicyEvaluator:
    """The Policy plane's run-immutable OPA-style evaluator. (governance §2)

    Default-deny: :meth:`evaluate` returns ``allow`` only when the subject names
    a known policy AND no deny rule fires. An unknown/missing policy, any deny
    rule, or an evaluator exception all DENY (fail-closed). The rule set is
    pinned at construction (run-immutable) — a run cannot edit its own rules.

    Production swaps this for OPA/Conftest/Checkov (ops change); the seam —
    fail-closed deny over a pinned rule set — is identical.
    """

    def __init__(self, rules: tuple[DenyRule, ...] = DENY_RULES) -> None:
        self._rules = rules

    @property
    def rules(self) -> tuple[DenyRule, ...]:
        return self._rules

    def evaluate(self, subject: Mapping[str, object]) -> PolicyDecision:
        for rule in self._rules:
            try:
                denied = rule.predicate(subject)
            except Exception as exc:  # evaluator error → fail-closed deny
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"policy evaluation error in rule {rule.name!r}: "
                    f"{type(exc).__name__}: {exc}",
                    matched_rule=rule.name,
                )
            if denied:
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"denied by rule {rule.name!r}: {rule.description}",
                    matched_rule=rule.name,
                )
        return PolicyDecision(
            decision=Decision.ALLOW,
            reason=f"no deny rule fired for policy {subject.get('policy')!r}",
        )

    def authorize_duties(self, req: DutyRequest) -> AuthorizationDecision:
        """Segregation-of-duties check (delegates to ``authorization``)."""
        return authorize_segregation_of_duties(req)
