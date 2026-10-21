"""Forged-results hardening of the two-zone gate runner (T-M4-03, gov §1c/§4).

Two seams defend a Phase-4 adversarial exit where an agent-influenced path tries
to fabricate a PASS it never earned:

* **Zone-B config re-derivation.** Zone B re-derives the bound ``scanner_config_rev``
  from the run-immutable ``PinnedScannerConfig`` (never trusts an agent-supplied
  config). A swapped/weakened config zone A ran under → attested FAIL, never PASS;
  a pinned config that itself fails to verify → ERROR (tamper, not a judgment).
* **Sealed observation channel.** Zone A seals the observation; zone B re-derives
  the tag. A tampered observation (FAIL swapped for PASS between zone A and zone B)
  → ERROR, never PASS. The genuine sealed observation → the correct verdict.

Every emitted verdict still verifies under the zone-B key and is admitted by the
REAL ``merge_gates`` (an agent could not have forged it).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from foundry.gates import (
    DevGateWriteVerifier,
    GateImpl,
    TwoZoneGateRunner,
    ZoneAObservation,
    ZoneBSigner,
    observe,
)
from foundry.gates.runner import (
    RAN_CONFIG_REV_KEY,
    seal_observation,
)
from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
SEAL_KEY = b"zone-a-seal-key-fedcba9876543210"
LINEAGE_REV = "lineage-rev-1"


# ---- pinned scanner config --------------------------------------------------


@dataclass(frozen=True)
class _Pinned:
    """Minimal run-immutable pinned config: ``verify()`` re-derives the revision
    and raises when the record no longer matches the pin."""

    pinned_revision: str
    record_version: str

    def verify(self) -> str:
        if not self.is_intact():
            raise ValueError("pinned scanner config does not match its revision")
        return self.pinned_revision

    def is_intact(self) -> bool:
        return self.pinned_revision == f"scanner-config-v{self.record_version}"


def _pinned(version: str = "1") -> _Pinned:
    return _Pinned(pinned_revision=f"scanner-config-v{version}", record_version=version)


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def verifier(records: GlobalRecords):
    v = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(v)
    try:
        yield v
    finally:
        reset_gate_write_verifier(token)


def _seq(node: str, against: str) -> Seq:
    return Seq(run_id="run-1", emission_epoch=1, superstep_index=0, node_id=node, local=0)


def _admitted(result) -> bool:
    return merge_gates([result], None) == [result]


# ---- a configurable test gate ----------------------------------------------


class _ConfigPinnedGate:
    """A class-2 gate whose zone-A observation reports the config it ran under.

    ``ran_rev`` is what zone A claims to have executed under; ``seal_key`` (when
    set) seals the observation. ``decide`` PASSes a clean observation — so any
    downgrade in these tests is the runner's hardening, not the gate.
    """

    name = "security"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(self, *, ran_rev: str | None, seal_key: bytes | None = None) -> None:
        self._ran_rev = ran_rev
        self._seal_key = seal_key

    def execute(self, workdir: Path) -> ZoneAObservation:
        structured: dict[str, object] = {}
        if self._ran_rev is not None:
            structured[RAN_CONFIG_REV_KEY] = self._ran_rev
        return observe(
            self.name,
            exit_codes=(0,),
            findings=(),
            structured=structured,
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS


# =====================================================================
# Zone-B config re-derivation
# =====================================================================


def test_clean_run_passes_with_rederived_config(tmp_path, records, verifier):
    """A run whose zone-A config matches the re-derived pin PASSes, attested."""
    pinned = _pinned()
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "ignored-string-rev", LINEAGE_REV,
        pinned_config=pinned,
    )
    gate: GateImpl = _ConfigPinnedGate(ran_rev=pinned.pinned_revision)
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    # Zone B bound the RE-DERIVED rev, not the plain string handed to the ctor.
    assert r.scanner_config_rev == pinned.pinned_revision
    assert r.scanner_config_rev != "ignored-string-rev"
    assert _admitted(r)


def test_forged_config_caught_not_pass(tmp_path, records, verifier):
    """Zone A ran under a config that does not match the pin → FAIL, never PASS.

    A forged/weakened scanner config cannot produce a passing attested verdict.
    """
    pinned = _pinned()
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        pinned_config=pinned,
    )
    # The agent path supplies a DIFFERENT (weakened) config rev than the pin.
    gate: GateImpl = _ConfigPinnedGate(ran_rev="weakened-attacker-config")
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL
    assert r.status is not GateStatus.PASS
    assert any("scanner-config mismatch" in f for f in r.findings)
    # Still attested — the FAIL is a real, foldable zone-B verdict.
    assert r.scanner_config_rev == pinned.pinned_revision
    assert _admitted(r)


def test_forged_config_on_integrated_digest_latches(tmp_path, records, verifier):
    """A config-mismatch FAIL on an integrated digest latches stable-FAIL."""
    pinned = _pinned()
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        pinned_config=pinned,
    )
    digest = "c" * 64
    gate: GateImpl = _ConfigPinnedGate(ran_rev="weakened-attacker-config")
    r = runner.run(gate, digest, _seq("security", digest), tmp_path)
    assert r.status is GateStatus.FAIL
    assert records.stable_fail_latched(digest, "security")


def test_tampered_pinned_config_is_error(tmp_path, records, verifier):
    """A pinned config that fails to verify → ERROR (tamper, not a judgment)."""
    pinned = _pinned()
    # Tamper: keep the old pinned_revision but mutate the record so verify() fails.
    tampered = replace(pinned, record_version="999")
    assert not tampered.is_intact()
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        pinned_config=tampered,
    )
    gate: GateImpl = _ConfigPinnedGate(ran_rev=None)
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR  # not FAIL (not a tree judgment), not PASS
    assert any("scanner-config tamper" in f for f in r.findings)
    assert _admitted(r)


def test_no_pinned_config_preserves_legacy_behavior(tmp_path, records, verifier):
    """No pinned config → the plain string rev binds, exactly as before."""
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
    )
    # A reported ran-rev is ignored when there is no pinned config to re-derive
    # against — the mismatch check is meaningful only against a re-derived pin.
    gate: GateImpl = _ConfigPinnedGate(ran_rev="whatever")
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    assert r.scanner_config_rev == "scanner-rev-1"
    assert _admitted(r)


# =====================================================================
# Sealed observation channel
# =====================================================================


def test_sealed_genuine_observation_passes(tmp_path, records, verifier):
    """A genuine sealed observation verifies → the correct verdict (PASS)."""
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        seal_key=SEAL_KEY,
    )
    gate: GateImpl = _ConfigPinnedGate(ran_rev=None, seal_key=SEAL_KEY)
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    assert _admitted(r)


class _TamperingGate:
    """Zone A produces a SEALED FAIL observation, then an agent-influenced path
    swaps it for a PASS observation (re-sealing is impossible without the key).

    Models the swap by emitting a fresh PASS-shaped observation re-sealed with a
    WRONG key (the attacker does not hold zone A's sealing secret).
    """

    name = "test"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def execute(self, workdir: Path) -> ZoneAObservation:
        # The honest zone-A observation is a FAIL (a failing test ran).
        honest_fail = observe(
            self.name,
            exit_codes=(1,),
            findings=("test failed",),
            seal_key=SEAL_KEY,
        )
        # Attacker swaps in a PASS-looking observation and re-seals with a key
        # they don't actually have (the WRONG key).
        forged_pass = observe(
            self.name,
            exit_codes=(0,),
            findings=(),
            seal_key=b"attacker-forged-key-000000000000",
        )
        # The swapped observation reaches zone B in place of the honest one.
        assert honest_fail.exit_codes == (1,)  # what zone A actually saw
        return forged_pass

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        # Decide PASS on a clean (exit 0) observation — so only the seal check
        # can stop the forged PASS.
        return GateStatus.PASS if obs.exit_codes == (0,) else GateStatus.FAIL


def test_tampered_observation_is_error_not_pass(tmp_path, records, verifier):
    """A swapped/forged observation fails the seal → ERROR, never PASS."""
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        seal_key=SEAL_KEY,
    )
    gate: GateImpl = _TamperingGate()
    r = runner.run(gate, "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR
    assert r.status is not GateStatus.PASS
    assert any("sealed-channel violation" in f for f in r.findings)
    assert _admitted(r)  # ERROR is still a trusted, foldable record


def test_unsealed_observation_on_sealed_channel_is_error(tmp_path, records, verifier):
    """On a sealed channel an observation with NO seal is rejected (ERROR)."""
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        seal_key=SEAL_KEY,
    )
    # The gate produces an UNSEALED observation (seal_key=None) on a sealed channel.
    gate: GateImpl = _ConfigPinnedGate(ran_rev=None, seal_key=None)
    r = runner.run(gate, "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR
    assert any("sealed-channel violation" in f for f in r.findings)


def test_body_rewrite_breaks_seal(tmp_path, records):
    """Rewriting any observation body field breaks the seal (covers whole obs)."""
    sealed = observe("test", exit_codes=(1,), findings=("boom",), seal_key=SEAL_KEY)
    # Flip the verdict-bearing fields to look like a PASS but keep the old seal.
    forged = sealed.model_copy(update={"exit_codes": (0,), "findings": ()})
    # Re-deriving the seal over the forged body does NOT match the kept tag.
    rederived = seal_observation(forged.model_copy(update={"seal": ""}), SEAL_KEY)
    assert rederived.seal != forged.seal


def test_seal_also_covers_results_digest(tmp_path, records):
    """A swapped results_digest (cache-laundering) breaks the seal."""
    sealed = observe("test", exit_codes=(0,), seal_key=SEAL_KEY)
    forged = sealed.model_copy(update={"results_digest": "0" * 64})
    rederived = seal_observation(forged.model_copy(update={"seal": ""}), SEAL_KEY)
    assert rederived.seal != forged.seal
