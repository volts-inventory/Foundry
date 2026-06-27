"""Non-agent, least-privileged, idempotent + rollback-capable deployer (T-M4-08).

This is the LAST mile of the trust root (governance.md §4/§5): a separate,
least-privileged, NON-AGENT component that ships ONLY an admitted artifact. It is
the deploy-time counterpart to the build trust root (`TrustedBuilder`) and the
deploy admission gate (`AdmissionController`):

  build (TrustedBuilder)  ->  admit (AdmissionController)  ->  deploy (Deployer)

The :class:`Deployer` consumes an :class:`AdmissionDecision` and, ONLY if it
``admitted()``, REQUESTS a deploy external effect for the EXACT digest the
admission verified. It does NOT itself touch any cloud/network: like every
external effect in Foundry, a deploy is an :class:`ExternalAction` REQUEST on the
``actions`` channel; the outbox relay is the sole component that reaches the
outside world (mirrors the ``open_pr`` action — architecture.md §5).

The four properties this module exists to enforce:

1. **Admitted-only (fail-closed).** :meth:`Deployer.deploy` REFUSES — raises
   :class:`DeployRefused` — when ``not decision.admitted()``. A DENY /
   INDETERMINATE decision is NEVER deployed; a deploy is bound to the EXACT
   ``subject_digest`` the admission verified (read FROM the decision, never a
   caller-supplied digest). An unverified/denied artifact can never ship.

2. **Idempotent.** The deploy's idempotency key is a DETERMINISTIC
   ``content_digest`` over ``(subject_digest, target)`` — NOT a wall clock, NOT
   Python's seed-randomized ``hash()`` (L7). Deploying the same admitted digest
   to the same target twice produces the SAME key, so a re-run / kill-then-resume
   coalesces (the keyed outbox / ``union_actions`` reducer dedups; mirrors the
   ``open_pr`` ExternalAction idempotency). Resume-safe by construction.

3. **Rollback.** :meth:`Deployer.rollback` rolls a target back to a prior-good
   ``subject_digest``: it emits a bound rollback deploy (the produced
   :class:`DeployResult` carries ``rollback_to_digest`` -> the prior subject and
   ``rolled_back=True``). A rollback is itself idempotent (its key folds in a
   ``rollback`` discriminator + the from/to digests + target) and bound to the
   admitted decision that authorized the prior-good digest.

4. **Non-agent / least-privilege.** The deployer runs NO agent code, holds NO
   signing key, and re-derives NO verdicts: it does not re-run admission, does not
   verify signatures, does not execute agent-authored code. Its SOLE capability is
   to emit the deploy/rollback :class:`ExternalAction`. It consumes a finished
   :class:`AdmissionDecision` (it trusts the admission controller's verdict bit,
   `admitted()`); it carries no signer/verifier/records dependency for the deploy
   itself. That narrow capability IS the least-privilege boundary.

DEV-GRADE ROOT (implementation-plan.md §0.3): the admitted-only + idempotency +
rollback DISCIPLINE here is REAL and CORRECT software. No real cloud deploy
happens — the deploy is a request; the relay's dispatch is the (injected, dev or
prod) transport. The least-privileged deployer's process/credential ISOLATION
boundary is an ops deployment concern, not coded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from foundry.canonical import content_digest
from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.schemas.trust_root import DeployResult

if TYPE_CHECKING:
    from foundry.schemas.trust_root import AdmissionDecision
    from foundry.state.seq import Seq

# The deploy ExternalAction kind (matches base.ExternalAction's documented set).
_KIND_DEPLOY = "deploy"

# Idempotency-key discriminators folded into the content_digest so a deploy and a
# rollback of the same (digest, target) can never collide on one key.
_OP_DEPLOY = "deploy"
_OP_ROLLBACK = "rollback"


class DeployRefused(RuntimeError):
    """The deployer refused to deploy a non-admitted artifact (fail-closed).

    Raised by :meth:`Deployer.deploy` / :meth:`Deployer.rollback` when the
    presented :class:`AdmissionDecision` is not ``admitted()`` (DENY /
    INDETERMINATE). A denied or unverified artifact is NEVER deployed
    (governance.md §5)."""


def _deploy_idempotency_key(
    *, op: str, subject_digest: str, target: str, from_digest: str | None
) -> str:
    """Process-stable deploy idempotency key (L7 — content_digest, never hash()).

    A pure function of the operation kind, the deployed ``subject_digest``, the
    ``target``, and (for a rollback) the ``from_digest`` being rolled back from —
    NO wall clock, NO per-process seed. So a kill-then-resume re-derives the SAME
    key and the keyed reducer / outbox coalesces the re-emission. The ``op``
    discriminator keeps a deploy and a rollback of the same (digest, target)
    distinct; ``from_digest`` keeps two rollbacks landing on the same digest from
    different priors distinct."""
    return content_digest(
        {
            "op": op,
            "subject_digest": subject_digest,
            "target": target,
            "from_digest": from_digest,
        }
    )


@dataclass(frozen=True)
class Deployer:
    """Non-agent, least-privileged, idempotent + rollback-capable deployer.

    Trusted infra (zone B): holds NO agent code, NO signing key, and reaches NO
    network. Its SOLE capability is to emit a deploy/rollback
    :class:`ExternalAction` for an artifact an :class:`AdmissionController` has
    already admitted — it re-derives no verdicts and signs nothing (see the module
    docstring's least-privilege posture).

    ``deploy`` and ``rollback`` are pure functions of their inputs: the same
    admitted decision + target always yields the same :class:`DeployResult` +
    :class:`ExternalAction` (same idempotency key), so a resume coalesces.

    ``target`` is the deploy destination identity (e.g. ``"prod"`` /
    ``"prod/us-east"``) — it is folded into the idempotency key so deploying one
    admitted digest to two distinct targets are two distinct (non-coalescing)
    effects, while re-deploying to the SAME target coalesces.
    """

    target: str

    def deploy(
        self,
        decision: AdmissionDecision,
        *,
        admission_decision_uri: str,
        seq: Seq,
        deploy_plan_uri: str | None = None,
    ) -> tuple[DeployResult, ExternalAction]:
        """Request a deploy of the admitted ``decision``'s subject (idempotent).

        Fail-closed: REFUSES (raises :class:`DeployRefused`) if
        ``not decision.admitted()`` — a denied / indeterminate decision is never
        deployed. The deploy is bound to the EXACT ``decision.subject_digest`` the
        admission verified (read FROM the decision, never supplied by the caller).

        Returns the :class:`DeployResult` execution record (the deploy identity,
        the deployed digest, the process-stable idempotency key, and the link to
        the authorizing :class:`AdmissionDecision`) AND the deploy
        :class:`ExternalAction` REQUEST to enqueue on the ``actions`` channel. NO
        real deploy happens here — the outbox relay is the sole component that
        touches the outside world.

        ``admission_decision_uri`` is the CAS handle to the authorizing decision
        (DS9), recorded on the result so the deploy is auditable back to its
        admission grounds. Deploying the same admitted digest to this target twice
        yields the SAME idempotency key, so the re-emission coalesces (resume-safe).
        """
        self._require_admitted(decision)
        subject_digest = decision.subject_digest
        key = _deploy_idempotency_key(
            op=_OP_DEPLOY,
            subject_digest=subject_digest,
            target=self.target,
            from_digest=None,
        )
        result = DeployResult(
            deploy_id=f"deploy:{self.target}:{subject_digest}",
            subject_digest=subject_digest,
            idempotency_key=key,
            admission_decision_uri=admission_decision_uri,
            rollback_to_digest=None,
            rolled_back=False,
            deploy_plan_uri=deploy_plan_uri,
            seq=seq,
        )
        action = self._deploy_action(key=key, payload_uri=admission_decision_uri, seq=seq)
        return result, action

    def rollback(
        self,
        decision: AdmissionDecision,
        *,
        from_digest: str,
        admission_decision_uri: str,
        seq: Seq,
        deploy_plan_uri: str | None = None,
    ) -> tuple[DeployResult, ExternalAction]:
        """Roll ``target`` back to the admitted ``decision``'s subject (idempotent).

        A rollback is itself a deploy of a PRIOR-GOOD ``subject_digest`` and obeys
        the SAME discipline: fail-closed (the prior-good digest must come from an
        ``admitted()`` :class:`AdmissionDecision` — a rollback target is never an
        unverified digest), bound to that exact digest, and idempotent. Its key
        folds in a ``rollback`` discriminator + the ``from_digest`` being rolled
        back FROM (so re-rolling the same from/to/target coalesces, while a deploy
        and a rollback of the same digit never collide).

        Returns the :class:`DeployResult` (``rollback_to_digest`` -> the prior
        subject, ``rolled_back=True``) and the rollback deploy
        :class:`ExternalAction` to enqueue.
        """
        self._require_admitted(decision)
        rollback_to = decision.subject_digest
        key = _deploy_idempotency_key(
            op=_OP_ROLLBACK,
            subject_digest=rollback_to,
            target=self.target,
            from_digest=from_digest,
        )
        result = DeployResult(
            deploy_id=f"rollback:{self.target}:{from_digest}->{rollback_to}",
            subject_digest=rollback_to,
            idempotency_key=key,
            admission_decision_uri=admission_decision_uri,
            rollback_to_digest=rollback_to,
            rolled_back=True,
            deploy_plan_uri=deploy_plan_uri,
            seq=seq,
        )
        action = self._deploy_action(key=key, payload_uri=admission_decision_uri, seq=seq)
        return result, action

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _require_admitted(decision: AdmissionDecision) -> None:
        """Fail-closed gate: only an ``admitted()`` decision may be deployed."""
        if not decision.admitted():
            raise DeployRefused(
                f"refusing to deploy non-admitted subject "
                f"{decision.subject_digest!r}: admission verdict is "
                f"{decision.verdict.value!r} ({decision.reason}) — a denied or "
                "indeterminate artifact is never deployed (governance.md §5)"
            )

    def _deploy_action(self, *, key: str, payload_uri: str, seq: Seq) -> ExternalAction:
        """Build the deploy/rollback ExternalAction REQUEST (no network here).

        A deploy is gate-bearing (a release/deploy effect, never path-3 dedup —
        DS-R9-F4) and rides the EMBEDDED_MARKER path: resume re-derives the same
        deterministic key and re-queries the target's deployed digest, mirroring
        the ``open_pr`` action."""
        return ExternalAction(
            idempotency_key=f"{_KIND_DEPLOY}:{key}",
            kind=_KIND_DEPLOY,
            payload_uri=payload_uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            gate_bearing=True,
            seq=seq,
        )
