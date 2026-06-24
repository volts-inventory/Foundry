"""Non-agent, least-privileged, idempotent + rollback-capable deployer (T-M4-08).

The deployer ships ONLY an admitted artifact, idempotently, with rollback, and
runs NO agent code (governance.md §4/§5). These tests prove the four properties:

- Admitted-only (fail-closed): a DENY / INDETERMINATE AdmissionDecision REFUSES
  (no action emitted); an ADMIT emits exactly one deploy action bound to the
  EXACT subject_digest the admission verified.
- Idempotent: deploying the same admitted digest to the same target twice yields
  the SAME idempotency key (the keyed reducer / outbox coalesces) — resume-safe.
- Rollback: a rollback to a prior-good admitted digest emits a BOUND rollback
  action and is itself idempotent.
- Non-agent / least-privilege: the deployer carries no signer/verifier/records
  dependency for the deploy — it consumes a finished AdmissionDecision and emits
  exactly one capability (the deploy/rollback ExternalAction), nothing else.
"""

from __future__ import annotations

import inspect

import pytest

from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.schemas.operations import AdmissionDecision, AdmissionVerdict, DeployResult
from foundry.state.seq import Seq
from foundry.trust_root import Deployer, DeployRefused

_DIGEST = "sha256:" + "a" * 64
_PRIOR = "sha256:" + "b" * 64
_TREE = "tree-abc"
_DECISION_URI = "cas://admission/decision-1"


def _seq(*, local: int = 0) -> Seq:
    return Seq(
        run_id=_TREE,
        emission_epoch=0,
        superstep_index=0,
        node_id="deploy",
        local=local,
    )


def _decision(
    *,
    verdict: AdmissionVerdict = AdmissionVerdict.ADMIT,
    subject_digest: str = _DIGEST,
    reason: str = "ok",
) -> AdmissionDecision:
    return AdmissionDecision(
        subject_digest=subject_digest,
        tree_sha=_TREE,
        verdict=verdict,
        reason=reason,
        signature_verified=verdict is AdmissionVerdict.ADMIT,
        all_required_gates_pass=verdict is AdmissionVerdict.ADMIT,
        required_gates=("spec", "build", "test"),
        seq=_seq(),
    )


@pytest.fixture
def deployer() -> Deployer:
    return Deployer(target="prod")


# -- 1. Admitted-only (fail-closed) -----------------------------------------


def test_denied_decision_refuses_no_action(deployer: Deployer) -> None:
    """A DENY decision REFUSES — no deploy action is emitted (fail-closed)."""
    denied = _decision(verdict=AdmissionVerdict.DENY, reason="build_signature_invalid")
    with pytest.raises(DeployRefused):
        deployer.deploy(denied, admission_decision_uri=_DECISION_URI, seq=_seq())


def test_indeterminate_decision_refuses(deployer: Deployer) -> None:
    """An INDETERMINATE decision (unreachable revocation) is also refused."""
    indet = _decision(verdict=AdmissionVerdict.INDETERMINATE, reason="revocation_unreachable")
    with pytest.raises(DeployRefused):
        deployer.deploy(indet, admission_decision_uri=_DECISION_URI, seq=_seq())


def test_admitted_emits_one_bound_action(deployer: Deployer) -> None:
    """An ADMIT emits exactly one deploy action bound to the subject_digest."""
    result, action = deployer.deploy(
        _decision(), admission_decision_uri=_DECISION_URI, seq=_seq()
    )
    assert isinstance(result, DeployResult)
    assert isinstance(action, ExternalAction)
    # Bound to the EXACT admitted digest — never a caller-supplied one.
    assert result.subject_digest == _DIGEST
    assert result.admission_decision_uri == _DECISION_URI
    assert result.rollback_to_digest is None
    assert result.rolled_back is False
    assert action.kind == "deploy"
    # The deployed digest is bound into the (content-derived) key.
    assert _DIGEST in action.idempotency_key or result.idempotency_key in action.idempotency_key


def test_deploy_binds_decision_digest_not_caller_digest(deployer: Deployer) -> None:
    """The deploy follows the decision's subject_digest, not any external value."""
    other = _decision(subject_digest=_PRIOR)
    result, _ = deployer.deploy(other, admission_decision_uri=_DECISION_URI, seq=_seq())
    assert result.subject_digest == _PRIOR


# -- 2. Idempotent ----------------------------------------------------------


def test_redeploy_same_digest_same_key(deployer: Deployer) -> None:
    """Deploying the same admitted digest to the same target twice coalesces."""
    r1, a1 = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())
    # Resume: a fresh Seq (bumped epoch) but the SAME inputs.
    r2, a2 = deployer.deploy(
        _decision(), admission_decision_uri=_DECISION_URI, seq=_seq(local=1)
    )
    assert r1.idempotency_key == r2.idempotency_key
    assert a1.idempotency_key == a2.idempotency_key  # the keyed reducer dedups


def test_distinct_target_distinct_key() -> None:
    """The same digest to two DISTINCT targets are two distinct effects."""
    a = Deployer(target="prod").deploy(
        _decision(), admission_decision_uri=_DECISION_URI, seq=_seq()
    )[1]
    b = Deployer(target="staging").deploy(
        _decision(), admission_decision_uri=_DECISION_URI, seq=_seq()
    )[1]
    assert a.idempotency_key != b.idempotency_key


def test_distinct_digest_distinct_key(deployer: Deployer) -> None:
    """Two different admitted digests to one target are distinct deploys."""
    a = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())[1]
    b = deployer.deploy(
        _decision(subject_digest=_PRIOR), admission_decision_uri=_DECISION_URI, seq=_seq()
    )[1]
    assert a.idempotency_key != b.idempotency_key


def test_idempotency_key_is_content_stable_not_hash(deployer: Deployer) -> None:
    """The key is a content_digest (hex), process-stable across runs (L7)."""
    r, _ = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())
    assert len(r.idempotency_key) == 64
    assert all(c in "0123456789abcdef" for c in r.idempotency_key)


# -- 3. Rollback ------------------------------------------------------------


def test_rollback_emits_bound_action(deployer: Deployer) -> None:
    """Rollback to a prior admitted digest emits a bound rollback action."""
    prior = _decision(subject_digest=_PRIOR)  # the prior-good admitted digest
    result, action = deployer.rollback(
        prior, from_digest=_DIGEST, admission_decision_uri=_DECISION_URI, seq=_seq()
    )
    assert result.rolled_back is True
    assert result.rollback_to_digest == _PRIOR
    assert result.subject_digest == _PRIOR  # we deploy the prior-good subject
    assert action.kind == "deploy"
    assert action.gate_bearing is True


def test_rollback_refuses_unadmitted_target(deployer: Deployer) -> None:
    """A rollback target must itself be admitted (fail-closed)."""
    denied = _decision(subject_digest=_PRIOR, verdict=AdmissionVerdict.DENY, reason="x")
    with pytest.raises(DeployRefused):
        deployer.rollback(
            denied, from_digest=_DIGEST, admission_decision_uri=_DECISION_URI, seq=_seq()
        )


def test_rollback_idempotent(deployer: Deployer) -> None:
    """Re-rolling the same from/to/target coalesces (same key)."""
    prior = _decision(subject_digest=_PRIOR)
    k1 = deployer.rollback(
        prior, from_digest=_DIGEST, admission_decision_uri=_DECISION_URI, seq=_seq()
    )[0].idempotency_key
    k2 = deployer.rollback(
        prior, from_digest=_DIGEST, admission_decision_uri=_DECISION_URI, seq=_seq(local=1)
    )[0].idempotency_key
    assert k1 == k2


def test_rollback_key_distinct_from_deploy_key(deployer: Deployer) -> None:
    """A deploy and a rollback of the same digest never collide on one key."""
    dep = deployer.deploy(
        _decision(subject_digest=_PRIOR), admission_decision_uri=_DECISION_URI, seq=_seq()
    )[1]
    rb = deployer.rollback(
        _decision(subject_digest=_PRIOR),
        from_digest=_DIGEST,
        admission_decision_uri=_DECISION_URI,
        seq=_seq(),
    )[1]
    assert dep.idempotency_key != rb.idempotency_key


# -- 4. Non-agent / least-privilege -----------------------------------------


def test_deployer_holds_no_signer_or_verifier() -> None:
    """The deployer carries no signer/verifier/records capability for the deploy.

    Its sole input is the target; it consumes a finished AdmissionDecision and
    re-derives nothing — it neither signs nor re-verifies."""
    fields = set(inspect.signature(Deployer).parameters)
    assert fields == {"target"}
    forbidden = {"signer", "verifier", "records", "key", "key_id", "transport"}
    assert not (fields & forbidden)


def test_deploy_signature_has_no_signing_inputs() -> None:
    """The deploy/rollback API takes a decision + handles + seq — no signer/key."""
    for fn in (Deployer.deploy, Deployer.rollback):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"signer", "verifier", "key", "private_key", "records"})


def test_deploy_emits_request_not_effect(deployer: Deployer) -> None:
    """A deploy is an ExternalAction REQUEST (outbox relay touches the world)."""
    _, action = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())
    assert action.path is IdempotencyPath.EMBEDDED_MARKER
    assert action.gate_bearing is True  # a deploy is gate-bearing, never path-3


# -- 5. Determinism ---------------------------------------------------------


def test_determinism_same_inputs_same_record(deployer: Deployer) -> None:
    """Same admitted decision + target + seq => identical DeployResult + action."""
    r1, a1 = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())
    r2, a2 = deployer.deploy(_decision(), admission_decision_uri=_DECISION_URI, seq=_seq())
    assert r1 == r2
    assert a1 == a2
