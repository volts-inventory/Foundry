"""Phase 3 (ROADMAP): verification profiles — "green must mean it works".

A profile is a named, pinned bundle of build/test/smoke commands selected from
the request's stack inference; the two-zone trust seam is unchanged. The
``python`` profile keeps every historical default (byte-identical gates); a
game profile adds a runtime SMOKE gate the boundary is then responsible for
(a FAILed or missing smoke can never advance); a missing toolchain surfaces as
a loud ERROR, never a silent PASS.
"""

from __future__ import annotations

from pathlib import Path

from foundry.gates.profiles import PROFILES, profile_for_request
from foundry.gates.smoke import SmokeGate
from foundry.schemas.gates import GateStatus

# ---- selection ----------------------------------------------------------------


def test_selection_mirrors_stack_inference() -> None:
    cases = {
        "a stardew-style farming game about colonizing the moon": "love2d",
        "a pygame roguelike": "pygame",
        "a phaser browser game with sprites": "phaser-web",
        "a tetris clone in javascript with html5 canvas": "phaser-web",
        "a file-encryption CLI tool": "crypto",
        "a password vault with e2ee sync": "crypto",
        "terraform stack for a three-tier aws infrastructure": "iac-terraform",
        "a react dashboard for sales analytics": "node-web",
        "a todo REST API with sqlite": "python",
    }
    for request, expected in cases.items():
        assert profile_for_request(request).name == expected, request


def test_python_profile_keeps_all_defaults() -> None:
    p = PROFILES["python"]
    assert p.build_cmd is None and p.test_cmd is None and p.smoke_cmd is None


def test_every_profile_has_a_contract() -> None:
    # The contract text is the profile's promise to the coding substrate: what
    # the gates will actually execute.
    for p in PROFILES.values():
        assert p.contract_text.startswith("GATES:"), p.name


# ---- smoke gate ------------------------------------------------------------------


def test_smoke_gate_pass_and_fail(tmp_path: Path) -> None:
    passing = SmokeGate(("/bin/sh", "-c", "exit 0"))
    failing = SmokeGate(("/bin/sh", "-c", "exit 3"))
    assert passing.decide(passing.execute(tmp_path)) is GateStatus.PASS
    assert failing.decide(failing.execute(tmp_path)) is GateStatus.FAIL


def test_smoke_gate_missing_toolchain_is_grounding_unavailable(tmp_path: Path) -> None:
    # A host without the pinned runtime must surface loudly (the runner signs
    # this as ERROR), never PASS silently.
    import pytest

    from foundry.gates.runner import GroundingUnavailable

    gate = SmokeGate(("definitely-not-a-real-runtime-xyz",))
    with pytest.raises(GroundingUnavailable):
        gate.execute(tmp_path)


# ---- boundary integration ----------------------------------------------------------


def test_smoke_boundary_includes_smoke_verdict() -> None:
    from foundry.graph.gate_wiring import (
        BUILD_TEST_BOUNDARY,
        BUILD_TEST_SMOKE_BOUNDARY,
        _BOUNDARY_GATES,
    )

    assert _BOUNDARY_GATES[BUILD_TEST_BOUNDARY] == frozenset({"build", "test"})
    assert _BOUNDARY_GATES[BUILD_TEST_SMOKE_BOUNDARY] == frozenset({"build", "test", "smoke"})


def test_profile_commands_reach_the_gates(tmp_path: Path, monkeypatch) -> None:
    # run_build_test_gates constructs the gates FROM the profile: a failing
    # pinned smoke fails its verdict while build/test pass on their pinned cmds.
    from tests.test_gate_wiring import (  # reuse the suite's real-runner harness
        KEY,
        KEY_ID,
        LINEAGE_REV,
        SCANNER_REV,
        rt as rt_fixture,  # noqa: F401  (fixture import registers it)
    )
    import tests.test_gate_wiring as tgw
    from foundry.gates import TwoZoneGateRunner, ZoneBSigner
    from foundry.gates.profiles import VerificationProfile
    from foundry.graph.gate_wiring import run_build_test_gates
    from foundry.state.global_records import GlobalRecords

    records = GlobalRecords()
    runner = TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)
    rt = tgw.make_rt() if hasattr(tgw, "make_rt") else None
    if rt is None:  # construct a minimal runtime the same way the suite does
        import pytest

        rt = pytest.importorskip("tests.test_gate_wiring").__dict__.get("_rt_instance")
    # Fall back: build the rt exactly like the fixture body does.
    if rt is None:
        from foundry.agents._runtime import AgentRuntime
        from foundry.proxy import ModelProviderProxy
        from foundry.proxy.test_doubles import MockTransport
        from foundry.state.seq import EpochCounter

        rt = AgentRuntime(
            proxy=ModelProviderProxy(transport=MockTransport()), epochs=EpochCounter()
        )

    profile = VerificationProfile(
        name="t",
        build_cmd=("/bin/sh", "-c", "exit 0"),
        test_cmd=("/bin/sh", "-c", "exit 0"),
        smoke_cmd=("/bin/sh", "-c", "exit 7"),
    )
    out = run_build_test_gates(
        {"artifacts": []},
        {"configurable": {"thread_id": "t-profile"}},
        rt=rt,
        runner=runner,
        resolve=lambda h: None,
        resolve_blob=lambda u: b"",
        workdir=tmp_path,
        profile=profile,
    )
    by_name = {g.name: g.status for g in out["gates"]}
    assert by_name["build"] is GateStatus.PASS
    assert by_name["test"] is GateStatus.PASS  # exit-code-only floor for foreign harness
    assert by_name["smoke"] is GateStatus.FAIL  # pinned boot command failed


def test_no_profile_runs_no_smoke_gate(tmp_path: Path) -> None:
    from foundry.gates import TwoZoneGateRunner, ZoneBSigner
    from foundry.agents._runtime import AgentRuntime
    from foundry.graph.gate_wiring import run_build_test_gates
    from foundry.proxy import ModelProviderProxy
    from foundry.proxy.test_doubles import MockTransport
    from foundry.state.global_records import GlobalRecords
    from foundry.state.seq import EpochCounter
    from tests.test_gate_wiring import KEY, KEY_ID, LINEAGE_REV, SCANNER_REV

    runner = TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), GlobalRecords(), SCANNER_REV, LINEAGE_REV
    )
    rt = AgentRuntime(proxy=ModelProviderProxy(transport=MockTransport()), epochs=EpochCounter())
    out = run_build_test_gates(
        {"artifacts": []},
        {"configurable": {"thread_id": "t-noprofile"}},
        rt=rt,
        runner=runner,
        resolve=lambda h: None,
        resolve_blob=lambda u: b"",
        workdir=tmp_path,
    )
    assert {g.name for g in out["gates"]} == {"build", "test"}
