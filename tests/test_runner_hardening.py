"""Forged-results hardening of the two-zone gate runner (T-M4-03, gov §1c/§4).

**Sealed observation channel.** Zone A seals the observation; zone B re-derives
the tag. A tampered observation (FAIL swapped for PASS between zone A and zone B)
→ ERROR, never PASS. The genuine sealed observation → the correct verdict.

Every emitted verdict still verifies under the zone-B key and is admitted by the
REAL ``merge_gates`` (an agent could not have forged it).
"""

from __future__ import annotations

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
from foundry.gates.runner import seal_observation
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


class _CleanGate:
    """A class-2 gate whose ``decide`` PASSes a clean observation — so any
    downgrade in these tests is the runner's hardening, not the gate.
    ``seal_key`` (when set) seals the observation.
    """

    name = "test"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(self, *, seal_key: bytes | None = None) -> None:
        self._seal_key = seal_key

    def execute(self, workdir: Path) -> ZoneAObservation:
        return observe(self.name, exit_codes=(0,), findings=(), seal_key=self._seal_key)

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS


# =====================================================================
# Sealed observation channel
# =====================================================================


def test_sealed_genuine_observation_passes(tmp_path, records, verifier):
    """A genuine sealed observation verifies → the correct verdict (PASS)."""
    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", LINEAGE_REV,
        seal_key=SEAL_KEY,
    )
    gate: GateImpl = _CleanGate(seal_key=SEAL_KEY)
    r = runner.run(gate, "task/1", _seq("test", "task/1"), tmp_path)
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
    gate: GateImpl = _CleanGate()
    r = runner.run(gate, "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR
    assert any("sealed-channel violation" in f for f in r.findings)


@pytest.mark.parametrize(
    ("observed", "forged_fields"),
    [
        # Flip the verdict-bearing body fields to look like a PASS.
        ({"exit_codes": (1,), "findings": ("boom",)}, {"exit_codes": (0,), "findings": ()}),
        # Swap the results_digest (cache-laundering).
        ({"exit_codes": (0,)}, {"results_digest": "0" * 64}),
    ],
)
def test_seal_covers_observation_fields(observed, forged_fields):
    """Rewriting any observation field while keeping the old seal breaks it."""
    sealed = observe("test", **observed, seal_key=SEAL_KEY)
    forged = sealed.model_copy(update=forged_fields)
    rederived = seal_observation(forged.model_copy(update={"seal": ""}), SEAL_KEY)
    assert rederived.seal != forged.seal
