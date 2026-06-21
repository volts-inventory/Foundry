"""Tests for the change-risk tiering classifier (governance.md §1e, T-M2-03).

Contract under test:

* LOW / STANDARD / HIGH are assigned on representative inputs.
* A security/abuse signal forces HIGH (forced_high).
* A regulated capability forces HIGH AND sets two_person_release.
* A trust/gate/oracle/scanner-config touch forces HIGH (and two_person_release).
* Ambiguity (empty request) escalates to STANDARD — never silently below the
  full integrated gate set (fail-closed; never a silent downgrade).
* `relaxes_integrity()` is False for EVERY tier (integrity never relaxed).
* Deterministic: same input -> identical decision.
"""

from __future__ import annotations

from foundry.governance.tiering import classify_tier
from foundry.schemas.governance import RiskTier, TierDecision
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=0, node_id="triage", local=0)


def _classify(request: str, **kw: object) -> TierDecision:
    return classify_tier(request, seq=SEQ, **kw)  # type: ignore[arg-type]


# --- tier assignment ---------------------------------------------------------


def test_trivial_change_is_low() -> None:
    d = _classify("Fix a typo in the README docstring")
    assert d.tier is RiskTier.LOW
    assert d.forced_high is False
    assert d.two_person_release is False
    # integrity-critical gates STILL present at LOW
    assert {"build", "test", "security"} <= set(d.gate_set)


def test_ordinary_feature_is_standard() -> None:
    d = _classify("Add a profile page that lists the user's recent posts")
    assert d.tier is RiskTier.STANDARD
    assert d.forced_high is False
    assert "spec" in d.gate_set and "review" in d.gate_set


# --- force-escalation: security / abuse --------------------------------------


def test_security_signal_forces_high() -> None:
    d = _classify("Refactor the auth login flow and rotate the JWT signing")
    assert d.tier is RiskTier.HIGH
    assert d.forced_high is True
    assert "supply_chain" in d.gate_set and "threat_review" in d.gate_set


def test_abuse_signal_forces_high() -> None:
    d = _classify("Patch the SQL injection vulnerability in search")
    assert d.tier is RiskTier.HIGH
    assert d.forced_high is True


def test_build_ci_dependency_signal_forces_high() -> None:
    # build/CI/IaC/deps are §1e HIGH triggers regardless of diff size
    for req in (
        "bump a dependency in requirements.txt",
        "edit the github actions workflow",
        "update the Dockerfile base image",
    ):
        d = _classify(req)
        assert d.tier is RiskTier.HIGH, req
        assert d.forced_high is True, req


# --- force-escalation: regulated capability ----------------------------------


def test_regulated_capability_forces_high_and_two_person() -> None:
    d = _classify(
        "Change the standard interest-rate display",  # tiny diff, regulated domain
        capabilities=["fintech"],
    )
    assert d.tier is RiskTier.HIGH
    assert d.forced_high is True
    assert d.two_person_release is True  # regulated -> distinct-identity sign-off


def test_regulated_capability_from_state_mapping() -> None:
    state = {
        "request": "tweak a label",
        "required_capabilities": ["healthcare"],
    }
    d = classify_tier(state, seq=SEQ)
    assert d.tier is RiskTier.HIGH
    assert d.two_person_release is True


def test_non_regulated_capability_does_not_force_high() -> None:
    d = _classify("Build a dashboard widget", capabilities=["ui"])
    assert d.tier is RiskTier.STANDARD
    assert d.forced_high is False


# --- force-escalation: trust/gate/oracle/scanner-config ----------------------


def test_trust_root_config_touch_forces_high_two_person() -> None:
    for req in (
        "update the scanner-config thresholds",
        "edit the gate config for the build runner",
        "rotate the signing key in the KMS",
        "change an oracle binding for the test gate",
    ):
        d = _classify(req)
        assert d.tier is RiskTier.HIGH, req
        assert d.forced_high is True, req
        assert d.two_person_release is True, req  # trust-root -> two-person


# --- ambiguity: fail-closed, never silent downgrade --------------------------


def test_empty_request_defaults_standard_not_low() -> None:
    d = _classify("")
    # ambiguity escalates to STANDARD (NOT a silent downgrade to LOW)
    assert d.tier is RiskTier.STANDARD
    assert d.forced_high is False


def test_security_signal_overrides_trivial_marker() -> None:
    # a trivial marker present alongside a security signal must NOT downgrade
    d = _classify("Just a typo fix in the password hashing crypto helper")
    assert d.tier is RiskTier.HIGH
    assert d.forced_high is True


# --- integrity is NEVER relaxed, for every tier ------------------------------


def test_relaxes_integrity_false_for_every_tier() -> None:
    decisions = [
        _classify("fix a typo"),  # LOW
        _classify("add a feature"),  # STANDARD
        _classify("rotate the signing key"),  # HIGH
        _classify("anything", capabilities=["regulated"]),  # HIGH regulated
        _classify(""),  # STANDARD (ambiguity)
    ]
    tiers = {d.tier for d in decisions}
    assert tiers == {RiskTier.LOW, RiskTier.STANDARD, RiskTier.HIGH}
    for d in decisions:
        assert d.relaxes_integrity() is False
        # integrity-critical gates run at every tier, fail-closed
        assert {"build", "test", "security"} <= set(d.gate_set)


# --- determinism -------------------------------------------------------------


def test_deterministic() -> None:
    req = "Refactor the auth login flow"
    a = _classify(req, capabilities=["fintech"])
    b = _classify(req, capabilities=["fintech"])
    assert a == b
    assert a.content_identity() == b.content_identity()


def test_str_and_state_inputs_agree() -> None:
    req = "rotate the signing key in the KMS"
    from_str = classify_tier(req, seq=SEQ)
    from_state = classify_tier({"request": req}, seq=SEQ)
    assert from_str == from_state
