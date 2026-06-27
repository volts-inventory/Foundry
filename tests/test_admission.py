"""Deploy-time admission control — the fail-closed trust-root deploy gate (T-M4-07).

The load-bearing trust gate: admission RE-VERIFIES, at deploy time, for the EXACT
subject digest, that (1) the provenance binds this subject, (2) the build
signature + every required gate attestation re-verifies under the trusted
verifier, (3) all required gates PASS bound to THIS integrated tree, and (4) the
digest is not cross-fork DISQUALIFIED / stable-FAIL latched. ADMIT only when ALL
hold; ANY missing/failed check ⇒ DENY (fail-closed).

The evidence is GENUINE: a real `TrustedBuilder.build` produces the signed,
provenance-bound build verdict; the required gate verdicts are signed by the same
zone-B `ZoneBSigner.with_attestation`. The verifier is the real
`DevGateWriteVerifier`; the disqualification query is the real `GlobalRecords`.

Cases: clean ADMIT, forged-verdict DENY, wrong-digest DENY (input-bound),
disqualified/fork-divergence DENY, missing-gate DENY, determinism, and the
grounds recorded on the AdmissionDecision.
"""

from __future__ import annotations

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.security import SBOMComponent
from foundry.schemas.trust_root import (
    AdmissionVerdict,
    BuildRequest,
)
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.trust_root import AdmissionController, BuildOutput, TrustedBuilder

KEY = b"admission-zone-b-key"
KEY_ID = "dev-admission-key"
BUILDER_ID = "dev-trusted-builder-1"

_SRC = "sha256:" + "c" * 64
_BASE = "sha256:" + "d" * 64
_MAT_A = "sha256:" + "a" * 64
_MAT_B = "sha256:" + "b" * 64

_REQUIRED_GATES = ("spec", "build", "test", "security")

_COMPONENTS = (
    SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT_A, license="MIT"),
    SBOMComponent(name="libbar", version="4.5.6", digest=_MAT_B, license="Apache-2.0"),
)


def _seq(run_id: str = "tree-abc", *, local: int = 0) -> Seq:
    return Seq(
        run_id=run_id,
        emission_epoch=0,
        superstep_index=0,
        node_id="admission",
        local=local,
    )


def _make_request(*, tree_sha: str = "tree-abc") -> BuildRequest:
    return BuildRequest(
        build_type="dev-build",
        source_uri="git://repo",
        source_digest=_SRC,
        tree_sha=tree_sha,
        base_image_digest=_BASE,
        build_config_uri="cas://invocation/1",
        material_digests=(_MAT_A, _MAT_B),
        hermetic=True,
        seq=_seq(tree_sha),
    )


@pytest.fixture
def signer() -> ZoneBSigner:
    return ZoneBSigner(KEY, KEY_ID)


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def verifier(records: GlobalRecords) -> DevGateWriteVerifier:
    return DevGateWriteVerifier(KEY, KEY_ID, records)


@pytest.fixture
def controller() -> AdmissionController:
    return AdmissionController(builder_id=BUILDER_ID)


@pytest.fixture
def build(signer: ZoneBSigner) -> BuildOutput:
    """A GENUINE hermetic build: signed, provenance-bound build verdict."""
    builder = TrustedBuilder(signer, builder_id=BUILDER_ID)
    return builder.build(_make_request(), components=_COMPONENTS)


def _signed_gate(
    signer: ZoneBSigner, name: str, tree_sha: str, *, local: int
) -> GateResult:
    """A genuine zone-B-attested PASS gate verdict bound to `tree_sha`."""
    g = GateResult(
        name=name,
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        results_digest=f"results-{name}",
        run_against=tree_sha,
        seq=_seq(tree_sha, local=local),
    )
    return signer.with_attestation(g)


def _required_gate_state(
    signer: ZoneBSigner, tree_sha: str
) -> tuple[GateResult, ...]:
    """Signed PASS verdicts for every required gate, bound to the tree."""
    return tuple(
        _signed_gate(signer, name, tree_sha, local=i)
        for i, name in enumerate(_REQUIRED_GATES)
    )


# ---------------------------------------------------------------------------
# Clean admit
# ---------------------------------------------------------------------------


def test_clean_admit(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.ADMIT
    assert decision.admitted()
    assert decision.reason == "ok"
    # the decision carries the EXACT digest + the integrated tree + grounds.
    assert decision.subject_digest == digest
    assert decision.tree_sha == tree
    assert decision.signature_verified
    assert decision.all_required_gates_pass
    assert decision.required_gates == _REQUIRED_GATES
    assert not decision.stable_fail_latched


# ---------------------------------------------------------------------------
# Forged-verdict DENY
# ---------------------------------------------------------------------------


def test_forged_gate_verdict_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """An unsigned (forged) required gate verdict ⇒ DENY (verify_attestation False)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    gates = list(_required_gate_state(signer, tree))
    # Drop the attestation off the `security` verdict (an agent-forged PASS).
    gates[3] = gates[3].model_copy(update={"runner_attestation": None})

    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gates),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_signature_invalid"
    assert not decision.all_required_gates_pass


def test_forged_build_signature_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A forged/unsigned BUILD provenance attestation ⇒ DENY."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    forged_build = build.signed_verdict.model_copy(
        update={"runner_attestation": "hmac256:dev-admission-key:deadbeef"}
    )
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=forged_build,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "build_signature_invalid"
    assert not decision.signature_verified


# ---------------------------------------------------------------------------
# Wrong-digest DENY (input-bound)
# ---------------------------------------------------------------------------


def test_wrong_subject_digest_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A provenance for a DIFFERENT digest re-presented for this subject ⇒ DENY
    (the provenance won't bind the requested subject)."""
    tree = build.provenance.tree_sha
    other_digest = "sha256:" + "e" * 64  # not what the provenance binds
    decision = controller.admit(
        other_digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "provenance_not_bound_to_subject"


def test_gate_verdict_for_other_tree_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A required gate verdict signed for a DIFFERENT tree ⇒ DENY (input-bound:
    the gate-state must be keyed to the provenance's exact tree)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    gates = list(_required_gate_state(signer, tree))
    # `test` verdict was genuinely signed, but for a different tree.
    gates[2] = _signed_gate(signer, "test", "some-other-tree", local=2)

    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gates),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_wrong_tree"


# ---------------------------------------------------------------------------
# Disqualified / fork-divergence DENY
# ---------------------------------------------------------------------------


def test_disqualified_digest_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A digest explicitly cross-fork DISQUALIFIED ⇒ DENY (forged/laundered fork
    can't be admitted)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    records.disqualify_digest(digest, "forged-verdict fork", "fork-run-99")

    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "stable_fail_latched_or_disqualified"
    assert decision.stable_fail_latched


def test_stable_fail_latched_digest_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A digest with a stable-FAIL latch (any gate) ⇒ DENY, even though the live
    gate-state shows PASS (gov §5: WORM holds it latched)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    records.latch_stable_fail(digest, "security", "run-7")

    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "stable_fail_latched_or_disqualified"


# ---------------------------------------------------------------------------
# Missing-gate DENY (fail-closed)
# ---------------------------------------------------------------------------


def test_missing_required_gate_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A required gate absent from the gate-state ⇒ DENY (fail-closed)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    # Omit the `security` verdict entirely.
    gates = tuple(
        _signed_gate(signer, name, tree, local=i)
        for i, name in enumerate(("spec", "build", "test"))
    )
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=gates,
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_missing"


def test_non_pass_required_gate_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A required gate present + attested but FAIL ⇒ DENY (signed FAIL, not PASS)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    gates = list(_required_gate_state(signer, tree))
    failing = GateResult(
        name="security",
        status=GateStatus.FAIL,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        results_digest="results-security",
        run_against=tree,
        seq=_seq(tree, local=3),
    )
    gates[3] = signer.with_attestation(failing)  # genuinely signed FAIL
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gates),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_not_pass"


# ---------------------------------------------------------------------------
# Scanner-config pin + tampered provenance
# ---------------------------------------------------------------------------


def test_scanner_config_mismatch_denied(
    controller: AdmissionController,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A provenance scanner-config rev != the pinned hash ⇒ DENY (gov §5)."""
    builder = TrustedBuilder(signer, builder_id=BUILDER_ID)
    build = builder.build(
        _make_request(), components=_COMPONENTS, scanner_config_rev="rev-A"
    )
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        scanner_config_rev="rev-B",  # pinned expectation differs
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "scanner_config_rev_mismatch"


def test_tampered_provenance_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A tampered provenance (different content_identity) still binding the same
    subject ⇒ DENY: the build signature no longer re-verifies."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    # Tamper a material digest — keeps subject_digest (still binds) but re-keys
    # content_identity(), so verify_provenance_binding fails.
    bad_materials = (
        *build.provenance.materials[:-1],
        build.provenance.materials[-1].model_copy(
            update={"digest": "sha256:" + "f" * 64}
        ),
    )
    tampered = build.provenance.model_copy(update={"materials": bad_materials})
    decision = controller.admit(
        digest,
        provenance=tampered,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "build_signature_invalid"


def test_wrong_builder_id_denied(
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A provenance from a builder the controller does not trust ⇒ DENY."""
    controller = AdmissionController(builder_id="some-other-builder")
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=_required_gate_state(signer, tree),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "provenance_not_bound_to_subject"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """The same inputs always yield the same AdmissionDecision (no agent code,
    no clock, no network)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    gates = _required_gate_state(signer, tree)
    kwargs = dict(
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=gates,
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    d1 = controller.admit(digest, **kwargs)  # type: ignore[arg-type]
    d2 = controller.admit(digest, **kwargs)  # type: ignore[arg-type]
    assert d1 == d2
    assert d1.verdict is AdmissionVerdict.ADMIT


def test_gate_order_independent(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """Gate-state order does not change the verdict (indexed by name)."""
    digest = build.result.subject_digest
    tree = build.provenance.tree_sha
    gates = list(_required_gate_state(signer, tree))
    reversed_gates = tuple(reversed(gates))
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=reversed_gates,
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.ADMIT


# ---------------------------------------------------------------------------
# Multi-tree re-verification (M4-R1-01): the TERMINAL gates run on a DISTINCT
# tree from the assurance gates (AW2 re-hash); admission must re-verify ALL of
# them against THEIR expected tree, not rest the terminal gates on the upstream
# emit guard. The provenance binds the assurance tree; the terminal gates
# (spec/build/test) are pinned to the terminal tree via `expected_gate_trees`.
# ---------------------------------------------------------------------------

# The terminal gates ran on a DISTINCT materialized tree from the assurance gates.
_TERMINAL_GATES = ("spec", "build", "test")
_ASSURANCE_GATES = ("security",)


def _two_tree_gate_state(
    signer: ZoneBSigner, *, assurance_tree: str, terminal_tree: str
) -> tuple[tuple[GateResult, ...], dict[str, str]]:
    """Signed PASS verdicts split across two trees + the per-gate expected-tree
    mapping (terminal gates on the terminal tree, assurance on the prov tree)."""
    gates = [
        _signed_gate(signer, name, terminal_tree, local=i)
        for i, name in enumerate(_TERMINAL_GATES)
    ]
    gates += [
        _signed_gate(signer, name, assurance_tree, local=10 + i)
        for i, name in enumerate(_ASSURANCE_GATES)
    ]
    expected = {name: terminal_tree for name in _TERMINAL_GATES}
    expected.update({name: assurance_tree for name in _ASSURANCE_GATES})
    return tuple(gates), expected


def test_multi_tree_clean_admit(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """Terminal gates on a DISTINCT tree + assurance gates on the provenance tree
    ⇒ ADMIT when each is bound to ITS expected tree (multi-tree path is additive;
    the provenance still binds the integrated subject)."""
    digest = build.result.subject_digest
    prov_tree = build.provenance.tree_sha
    terminal_tree = "sha256:" + "7" * 64
    gates, expected = _two_tree_gate_state(
        signer, assurance_tree=prov_tree, terminal_tree=terminal_tree
    )
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=gates,
        required_gates=(*_TERMINAL_GATES, *_ASSURANCE_GATES),
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.ADMIT
    assert decision.all_required_gates_pass


def test_multi_tree_forged_terminal_gate_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """An agent-FORGED (unsigned) TERMINAL gate (build) ⇒ DENY. Previously the
    terminal gates were NOT re-verified by admission (only the emit guard) — now
    a forged terminal verdict fails admission's signature re-check."""
    digest = build.result.subject_digest
    prov_tree = build.provenance.tree_sha
    terminal_tree = "sha256:" + "7" * 64
    gates, expected = _two_tree_gate_state(
        signer, assurance_tree=prov_tree, terminal_tree=terminal_tree
    )
    gates = list(gates)
    # Strip the attestation off the `build` (terminal) verdict — agent-forged.
    idx = next(i for i, g in enumerate(gates) if g.name == "build")
    gates[idx] = gates[idx].model_copy(update={"runner_attestation": None})

    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gates),
        required_gates=(*_TERMINAL_GATES, *_ASSURANCE_GATES),
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_signature_invalid"


def test_multi_tree_missing_terminal_gate_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A TERMINAL gate (test) absent from the gate-state ⇒ DENY (fail-closed)."""
    digest = build.result.subject_digest
    prov_tree = build.provenance.tree_sha
    terminal_tree = "sha256:" + "7" * 64
    gates, expected = _two_tree_gate_state(
        signer, assurance_tree=prov_tree, terminal_tree=terminal_tree
    )
    gates = tuple(g for g in gates if g.name != "test")  # drop the terminal `test`
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=gates,
        required_gates=(*_TERMINAL_GATES, *_ASSURANCE_GATES),
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_missing"


def test_multi_tree_terminal_gate_wrong_tree_denied(
    controller: AdmissionController,
    build: BuildOutput,
    signer: ZoneBSigner,
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
) -> None:
    """A genuinely-signed TERMINAL gate bound to the WRONG tree (not its expected
    terminal tree) ⇒ DENY — admission's input-binding now covers the terminal
    gates against their own tree, not just the assurance group."""
    digest = build.result.subject_digest
    prov_tree = build.provenance.tree_sha
    terminal_tree = "sha256:" + "7" * 64
    gates, expected = _two_tree_gate_state(
        signer, assurance_tree=prov_tree, terminal_tree=terminal_tree
    )
    gates = list(gates)
    # `spec` genuinely signed, but for a THIRD tree (not its expected terminal one).
    idx = next(i for i, g in enumerate(gates) if g.name == "spec")
    gates[idx] = _signed_gate(signer, "spec", "sha256:" + "9" * 64, local=99)
    decision = controller.admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gates),
        required_gates=(*_TERMINAL_GATES, *_ASSURANCE_GATES),
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_wrong_tree"
