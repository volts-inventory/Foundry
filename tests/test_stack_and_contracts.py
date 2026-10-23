"""The planner chooses the stack and designs contracts first.

* the toolchain probe offers the languages this host has;
* the planner writes the ``stack`` channel once (a ``--profile`` pin wins), and
  falls back to the request-inferred profile for an undetected language;
* code contracts become a wave-0 ``contracts`` ticket whose files are gate-owned,
  a ``bdd`` stack gets a ``bdd-harness`` ticket, every other ticket waits on both;
* the contract fit check FAILs the Spec gate with findings the revision reads;
* the gates read the stack at gate time (typecheck runs inside BUILD, a stack
  without ``run`` records an attested "not pinned" smoke PASS);
* personas take the stack's language and Cucumber runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.agents._common import emit_artifact
from foundry.agents.implementation.personas import persona_for
from foundry.agents.planner import _plan_schema, make_planner_node
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.profiles import PROFILES, pinned_stack, stack_profile
from foundry.gates.spec import SpecGate
from foundry.graph.gate_wiring import (
    FEATURE_DIFF_KIND,
    TICKET_KIND,
    gate_profile,
    materialize_workdir,
    run_build_test_gates,
)
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    HARNESS_TICKET_ID,
    FeatureDiff,
    Specialty,
    StackDecision,
    Ticket,
    Toolchain,
)
from foundry.schemas.product import CriteriaCritique
from foundry.state.seq import Seq
from foundry.toolchains import _version, detect_toolchains
from tests.test_planner import PLAN, PYTHON_STACK, config_for, runtime_for

TOOLS = (
    Toolchain(language="python", binaries=("python3",), version="3.12"),
    Toolchain(language="typescript", binaries=("node", "npm"), version="v22"),
)

TS_STACK: dict[str, Any] = {
    "language": "typescript", "framework": "express", "package_manager": "npm",
    "install": "npm ci", "build": "npm run build", "typecheck": "npx tsc --noEmit",
    "test": "npm test", "bdd": "npx cucumber-js --format message:$FOUNDRY_BDD_REPORT",
    "run": "", "notes": "", "contracts_file": "src/contracts.ts",
    "harness_files": ["package.json", "tsconfig.json", "cucumber.json"],
}

TS_INTERFACES = [
    {"name": "TodoStore.add", "kind": "function", "signature": "add(todo: Todo): string",
     "inputs": ["todo: Todo"], "outputs": ["id: string"], "errors": ["TitleTooLong"],
     "description": "Stores a todo.", "consumers": ["api"]},
    {"name": "TodoStore.list", "kind": "function", "signature": "list(): Todo[]",
     "inputs": [], "outputs": ["todos: Todo[]"], "errors": [], "description": "All todos."},
    {"name": "POST /todos", "kind": "http", "signature": "POST /todos {title} -> 201 {id}",
     "inputs": [], "outputs": [], "errors": ["401"], "description": "Add over HTTP."},
]


def _ts_plan() -> dict[str, Any]:
    tickets = json.loads(json.dumps(PLAN["tickets"]))
    tickets[1]["files"] = ["todo/api.ts", "src/contracts.ts"]
    return {**PLAN, "stack": TS_STACK, "interfaces": TS_INTERFACES, "tickets": tickets}


def _plan_once(
    payload: dict[str, Any], state: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ArtifactStore, str]:
    cas = ArtifactStore()
    rt, tx = runtime_for(payload, cas=cas)
    writes = make_planner_node(rt, toolchains=TOOLS)(
        {"request": "build a todo app", **(state or {})}, config_for("r")
    )
    return writes, cas, json.dumps(tx.requests[-1], ensure_ascii=False)


def _tickets(writes: dict[str, Any], cas: ArtifactStore) -> list[Ticket]:
    return [cas.resolve(a) for a in writes["artifacts"] if a.kind == "ticket"]


def _critique(writes: dict[str, Any], cas: ArtifactStore) -> CriteriaCritique:
    [c] = [cas.resolve(a) for a in writes["artifacts"] if a.kind == "criteria_findings"]
    return c


# ---- §1 toolchain probe ----------------------------------------------------------


def test_probe_reports_languages_whose_binaries_are_present() -> None:
    paths = {"node": "/x/node", "npm": "/x/npm", "go": "/x/go", "java": "/x/java"}
    found = detect_toolchains(which=paths.get, version=lambda p: f"v-{p}")
    assert [t.language for t in found] == ["typescript", "javascript", "go"]
    assert found[0].binaries == ("node", "npm") and found[0].version == "v-/x/node"
    # java needs a build tool; python is absent when the injected PATH lacks it
    with_gradle = detect_toolchains(
        which={**paths, "gradle": "/x/gradle"}.get, version=lambda p: ""
    )
    assert [t.binaries for t in with_gradle if t.language == "java"] == [("java", "gradle")]


def test_version_probe_is_fail_soft() -> None:
    assert _version("/nonexistent/binary") == ""


# ---- §2 the planner chooses the stack -----------------------------------------------


def test_planner_writes_the_chosen_stack_and_offers_the_detected_languages() -> None:
    writes, _, prompt = _plan_once({**PLAN, "stack": TS_STACK})
    stack = writes["stack"]
    assert stack.language == "typescript" and stack.typecheck == "npx tsc --noEmit"
    assert stack.bdd.startswith("npx cucumber-js") and stack.profile == ""
    assert "LANGUAGES AVAILABLE HERE" in prompt and "typescript (node, npm; v22)" in prompt
    assert "EXAMPLE GATE COMMANDS" in prompt and "go vet ./..." in prompt
    enum = _plan_schema(["typescript", "python"])["properties"]["stack"]["properties"]
    assert enum["language"]["enum"] == ["python", "typescript"]


def test_undetected_language_writes_no_stack_and_says_so() -> None:
    writes, _, _ = _plan_once({**PLAN, "stack": {**TS_STACK, "language": "rust"}})
    assert "stack" not in writes
    assert any("'rust' is not available" in c for c in writes["agent_journal"][0].concerns)


def test_a_pinned_stack_is_kept_and_shown_as_fixed() -> None:
    pinned = pinned_stack("go")
    writes, _, prompt = _plan_once({**PLAN, "stack": TS_STACK}, {"stack": pinned})
    assert "stack" not in writes
    assert "STACK IS FIXED" in prompt and "go vet ./... && go build ./..." in prompt


def test_pinned_stack_mirrors_its_profile() -> None:
    s = pinned_stack("node-web")
    assert (s.language, s.profile, s.test) == ("javascript", "node-web", "npm test --silent")
    assert gate_profile({"stack": s}, None) is PROFILES["node-web"]


# ---- §2 / §4 generated tickets -------------------------------------------------------


def test_code_contracts_and_bdd_stack_generate_wave_zero_tickets() -> None:
    writes, cas, _ = _plan_once(_ts_plan())
    tickets = _tickets(writes, cas)
    contracts, harness, *rest = tickets
    assert contracts.id == CONTRACTS_TICKET_ID and contracts.depends_on == ()
    assert contracts.files == ("src/contracts.ts",)
    assert contracts.implements == ("TodoStore.add", "TodoStore.list")  # not the http one
    assert contracts.specialty is Specialty.BACKEND and contracts.difficulty.value == "trivial"
    assert harness.id == HARNESS_TICKET_ID and harness.specialty is Specialty.TEST
    assert harness.files == ("package.json", "tsconfig.json", "cucumber.json")
    assert harness.depends_on == () and "$FOUNDRY_BDD_REPORT" in harness.description
    for t in rest:
        assert t.depends_on[:2] == (CONTRACTS_TICKET_ID, HARNESS_TICKET_ID)
        assert "src/contracts.ts" not in t.files
    assert [t.seq.local for t in tickets] == list(range(len(tickets)))
    concerns = writes["agent_journal"][0].concerns
    assert any("T-002: dropped the gate-owned contract file(s) src/contracts.ts" in c
               for c in concerns)


def test_contracts_carry_kind_signature_and_a_structured_body() -> None:
    writes, cas, _ = _plan_once(_ts_plan())
    contracts = {
        c.name: c for c in (cas.resolve(a) for a in writes["artifacts"])
        if isinstance(c, InterfaceContract)
    }
    add = contracts["TodoStore.add"]
    assert (add.kind, add.signature) == ("function", "add(todo: Todo): string")
    body = cas.get_body(add.spec_uri).text
    assert body.startswith("[function] add(todo: Todo): string\nStores a todo.")
    assert "errors: TitleTooLong" in body


def test_python_stack_without_bdd_or_code_contracts_generates_nothing() -> None:
    writes, cas, _ = _plan_once({**PLAN, "stack": PYTHON_STACK})
    assert [t.id for t in _tickets(writes, cas)] == ["T-001", "T-002", "T-003"]


def test_a_model_ticket_cannot_take_a_generated_id() -> None:
    tickets = json.loads(json.dumps(PLAN["tickets"]))
    tickets[0]["id"] = "contracts"
    tickets[1]["depends_on"] = ["contracts"]
    writes, cas, _ = _plan_once({**_ts_plan(), "tickets": tickets})
    ids = [t.id for t in _tickets(writes, cas)]
    assert ids[:2] == [CONTRACTS_TICKET_ID, HARNESS_TICKET_ID] and "contracts-2" in ids


# ---- §4 contract fit check -----------------------------------------------------------


def test_clean_contract_fit_has_no_findings() -> None:
    writes, cas, _ = _plan_once(_ts_plan())
    assert _critique(writes, cas).contract_findings == ()


def test_fit_violations_become_blocking_spec_findings() -> None:
    tickets = json.loads(json.dumps(PLAN["tickets"]))
    tickets[1]["depends_on"] = []  # consumer not ordered after its producer
    tickets[1]["consumes"] = ["TodoStore.add", "TodoStore.missing"]
    tickets[2]["implements"] = ["TodoStore.list"]  # a second producer
    interfaces = [*TS_INTERFACES, {"name": "TodoStore.missing", "kind": "function",
                                   "signature": "m(): void", "inputs": [], "outputs": [],
                                   "errors": [], "description": ""},
                  {"name": "Unused", "kind": "type", "signature": "type U = 1",
                   "inputs": [], "outputs": [], "errors": [], "description": ""}]
    tickets[2]["implements"].append("Unused")
    writes, cas, _ = _plan_once({**_ts_plan(), "tickets": tickets, "interfaces": interfaces})
    findings = _critique(writes, cas).contract_findings
    assert "Contract TodoStore.list is implemented by 2 tickets (T-001, T-003); exactly " \
        "one ticket may implement it." in findings
    assert "Ticket T-002 consumes contract TodoStore.missing, but no ticket implements it." \
        in findings
    assert "Ticket T-002 consumes contract TodoStore.add but does not depend on T-001, " \
        "which implements it." in findings
    unused = "Contract Unused (implemented by T-003) has no consumer"
    assert any(f.startswith(unused) for f in findings)
    critique = _critique(writes, cas)
    assert critique.spec_gate_blocking()
    assert writes["gate_triage"][0].explanation.startswith(
        "BLOCKING: the contract fit check failed"
    )


def test_spec_gate_fails_on_contract_findings_and_names_them(tmp_path: Path) -> None:
    doc = {
        "criteria": [{"id": "AC-1", "text": "t", "oracle": "o", "security_abuse": True}],
        "critique": {"security_abuse_omissions": [],
                     "contract_findings": ["Ticket T-2 consumes contract X, but no ticket "
                                           "implements it."]},
    }
    (tmp_path / "criteria.json").write_text(json.dumps(doc))
    gate = SpecGate()
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.findings == (
        "contract fit: Ticket T-2 consumes contract X, but no ticket implements it.",
    )


# ---- gates read the stack at gate time -------------------------------------------------


def _js(**kw: str) -> StackDecision:
    return StackDecision(language="javascript", **kw)


def test_gate_profile_reads_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry.toolchains as tc

    monkeypatch.setattr(tc, "available_languages", lambda: frozenset({"javascript"}))
    fallback = PROFILES["python"]
    assert gate_profile({}, fallback) is fallback
    p = gate_profile({"stack": _js(install="npm ci", build="npm run build",
                                   typecheck="npx tsc --noEmit", test="npm test",
                                   bdd="npx cucumber-js")}, fallback)
    assert p is not None
    assert p.install_cmd == "npm ci"  # its own networked step, not folded into build
    assert p.build_cmd == ("/bin/sh", "-c", "npm run build && npx tsc --noEmit")
    assert p.test_cmd == ("/bin/sh", "-c", "npm test") and p.smoke_cmd is None
    assert p.bdd_cmd == "npx cucumber-js"
    bdd_only = gate_profile({"stack": _js(bdd="npx cucumber-js")}, fallback)
    assert bdd_only is not None and bdd_only.test_cmd == ("/bin/sh", "-c", "true")
    undetected = {"request": "a react dashboard", "stack": StackDecision(language="rust")}
    assert gate_profile(undetected, fallback) is PROFILES["node-web"]


def test_python_stack_keeps_the_trusted_harness() -> None:
    plain = stack_profile(StackDecision(language="python", test="pytest", install="pip i x"))
    assert (plain.build_cmd, plain.test_cmd, plain.install_cmd) == (None, None, None)
    typed = stack_profile(StackDecision(language="python", typecheck="python3 -m mypy ."))
    assert typed.build_cmd is not None and typed.build_cmd[2].endswith(
        "-m compileall -q . && python3 -m mypy ."
    )


def _runner_rt() -> tuple[Any, Any, Any]:
    from foundry.agents._runtime import AgentRuntime
    from foundry.gates import TwoZoneGateRunner, ZoneBSigner
    from foundry.proxy import ModelProviderProxy
    from foundry.proxy.test_doubles import MockTransport
    from foundry.state.global_records import GlobalRecords

    key = b"zone-b-dev-key-0123456789abcdef"
    runner = TwoZoneGateRunner(ZoneBSigner(key, "k"), GlobalRecords(), "s", "l")
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport(), signer=ZoneBSigner(key, "k")))
    config: Any = {"configurable": {"thread_id": "run-1"}, "metadata": {"langgraph_step": 1}}
    return runner, rt, config


def test_build_test_gates_run_the_stack_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.toolchains as tc

    monkeypatch.setattr(tc, "available_languages", lambda: frozenset({"javascript"}))
    runner, rt, config = _runner_rt()
    state: dict[str, Any] = {"artifacts": [], "stack": _js(
        build="true", typecheck="echo typed > typed.txt", test="true", run="exit 3",
    )}
    out = run_build_test_gates(
        state, config, rt=rt, runner=runner, resolve=lambda h: None,
        resolve_blob=lambda u: b"", workdir=tmp_path,
    )
    by_name = {g.name: g.status for g in out["gates"]}
    assert by_name == {"build": GateStatus.PASS, "test": GateStatus.PASS,
                       "smoke": GateStatus.FAIL}
    assert (tmp_path / "typed.txt").read_text().strip() == "typed"  # typecheck ran in BUILD


def _constructed_gates(
    state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    profile: Any = None,
) -> dict[str, Any]:
    """The Build/Test gates run_build_test_gates constructs (they still run)."""
    import foundry.graph.gate_wiring as gw

    made: dict[str, Any] = {}

    class _Build(gw.BuildGate):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made["build"] = self

    class _Test(gw.TestGate):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made["test"] = self

    monkeypatch.setattr(gw, "BuildGate", _Build)
    monkeypatch.setattr(gw, "TestGate", _Test)
    runner, rt, config = _runner_rt()
    run_build_test_gates(
        {"artifacts": [], **state}, config, rt=rt, runner=runner, resolve=lambda h: None,
        resolve_blob=lambda u: b"", workdir=tmp_path, profile=profile,
    )
    return made


def test_stack_on_state_drives_install_and_bdd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.toolchains as tc

    monkeypatch.setattr(tc, "available_languages", lambda: frozenset({"javascript"}))
    stack = _js(install="true", build="true", typecheck="true", test="true",
                bdd="npx cucumber-js --format message:$FOUNDRY_BDD_REPORT")
    made = _constructed_gates({"stack": stack}, tmp_path, monkeypatch)
    assert made["build"].install_cmd == "true"
    assert made["build"].cmd == ("/bin/sh", "-c", "true && true")
    test = made["test"]
    assert test.cmd == ("/bin/sh", "-c", "true") and test.min_passed == 0
    assert test.bdd_cmd == stack.bdd


def test_python_stack_without_bdd_keeps_the_default_test_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.toolchains as tc

    monkeypatch.setattr(tc, "available_languages", lambda: frozenset({"python"}))
    made = _constructed_gates(
        {"stack": StackDecision(language="python", test="pytest")}, tmp_path, monkeypatch
    )
    assert made["build"].install_cmd is None
    test = made["test"]
    assert test.bdd_cmd is None and test.min_passed == 1 and test._runs_bdd


def test_no_stack_falls_back_to_the_graph_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    fallback = replace(PROFILES["node-web"], install_cmd="true", build_cmd=("true",),
                       test_cmd=("true",), smoke_cmd=None)
    made = _constructed_gates({}, tmp_path, monkeypatch, profile=fallback)
    assert made["build"].install_cmd == "true" and made["build"].cmd == ("true",)
    assert made["test"].bdd_cmd is None and made["test"].cmd == ("true",)


def test_wave_build_check_reads_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry.toolchains as tc
    from foundry.graph.gate_wiring import WaveBuildCheck

    monkeypatch.setattr(tc, "available_languages", lambda: frozenset({"javascript"}))
    runner, rt, config = _runner_rt()
    check = WaveBuildCheck(rt, runner, lambda h: None, lambda u: b"")
    state: Any = {"artifacts": [], "stack": _js(typecheck="exit 2")}
    [build] = check.run(state, config)["gates"]
    assert build.name == "build" and build.status is GateStatus.FAIL


# ---- materialization: gate-owned contracts, no Python manifest for other stacks ------


def _seq(node: str, epoch: int, local: int = 0) -> Seq:
    return Seq(run_id="r", emission_epoch=epoch, superstep_index=0, node_id=node, local=local)


def _diff_state(
    diffs: list[tuple[str, dict[str, str]]], stack: StackDecision | None = None
) -> tuple[dict[str, Any], Any, Any]:
    by_uri: dict[str, Any] = {}
    blobs: dict[str, str] = {}
    contracts = Ticket(id=CONTRACTS_TICKET_ID, title="Define contracts",
                       specialty=Specialty.BACKEND, files=("src/contracts.ts",),
                       seq=_seq("planner", 1))
    h = emit_artifact(contracts, TICKET_KIND, "planner", contracts.seq)
    by_uri[h.uri] = contracts
    artifacts: list[Artifact] = [h]
    for i, (task, files) in enumerate(diffs, start=1):
        diff = FeatureDiff(diff_uri=f"cas://blob/{i}", task_id=task, branch=f"task/{task}",
                           base_tree_sha="b", seq=_seq("engineering", i))
        a = Artifact(kind=FEATURE_DIFF_KIND, uri=f"cas://fd/{i}", produced_by="engineering",
                     task_id=task, seq=diff.seq)
        artifacts.append(a)
        by_uri[a.uri] = diff
        blobs[diff.diff_uri] = json.dumps({"kind": "full-file-snapshot", "files": files})
    state: dict[str, Any] = {"artifacts": artifacts}
    if stack is not None:
        state["stack"] = stack
    return state, (lambda a: by_uri[a.uri]), (lambda u: blobs[u])


def test_contract_files_are_rewritten_from_the_contracts_ticket(tmp_path: Path) -> None:
    state, resolve, blob = _diff_state([
        (CONTRACTS_TICKET_ID, {"src/contracts.ts": "export type Id = string;\n"}),
        ("T-001", {"src/contracts.ts": "export type Id = number;\n", "src/app.ts": "x\n"}),
        ("fix-1", {"src/contracts.ts": "// gone\n"}),
    ])
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    assert (tmp_path / "src/contracts.ts").read_text() == "export type Id = string;\n"
    assert (tmp_path / "src/app.ts").read_text() == "x\n"


def test_a_retried_contracts_ticket_still_owns_its_files(tmp_path: Path) -> None:
    state, resolve, blob = _diff_state([
        (CONTRACTS_TICKET_ID, {"src/contracts.ts": "v1\n"}),
        (CONTRACTS_TICKET_ID, {"src/contracts.ts": "v2\n"}),
    ])
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    assert (tmp_path / "src/contracts.ts").read_text() == "v2\n"


def test_no_python_manifest_is_scaffolded_for_another_stack(tmp_path: Path) -> None:
    files = {"tool.py": "import requests\n", "src/app.ts": "x\n"}
    state, resolve, blob = _diff_state([("T-001", files)], _js())
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    assert not (tmp_path / "requirements.txt").exists()
    py, py_resolve, py_blob = _diff_state([("T-001", files)], StackDecision(language="python"))
    materialize_workdir(py, tmp_path / "py", resolve=py_resolve, resolve_blob=py_blob)
    assert (tmp_path / "py" / "requirements.txt").exists()


# ---- personas --------------------------------------------------------------------------


def test_personas_take_the_stack_wording() -> None:
    ts = StackDecision(language="typescript", framework="express",
                       bdd="npx cucumber-js --format message:$FOUNDRY_BDD_REPORT")
    test = persona_for("test", ts)
    assert "Cucumber runner for typescript" in test and "pytest" not in test
    assert "npx cucumber-js" in test and test.endswith("written in typescript with express.")
    assert "pytest-bdd" in persona_for("test", StackDecision(language="python"))
    assert persona_for("backend", ts).endswith("The project is written in typescript with express.")
    assert persona_for("backend") == persona_for("backend", None)


# ---- epic mode: a targeted re-plan keeps the generated tickets ------------------------


def test_targeted_replan_carries_the_contracts_ticket() -> None:
    from foundry.agents.implementation.tickets import read_tickets
    from foundry.proxy.test_doubles import ResponseScript
    from tests.planner_payload import multi_epic_plan
    from tests.test_planner import _epic_of, _epic_runtime, _epic_scripts, _failed, _match

    plan, replies = multi_epic_plan()
    plan["interfaces"] = [
        {**i, "kind": "function", "signature": i["spec"]} for i in plan["interfaces"]
    ]
    plan["stack"] = {**PYTHON_STACK, "contracts_file": "notes/contracts.py"}
    cas = ArtifactStore()
    revised = ResponseScript(json.dumps(replies["E2"]), match=_match("YOUR EPIC: E2", "REVISION"))
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies, revised), cas=cas)
    node = make_planner_node(rt, toolchains=TOOLS)
    first = node({"request": "x"}, config_for("r"))
    first_ids = [t.id for t in _tickets(first, cas)]
    assert first_ids[0] == CONTRACTS_TICKET_ID and HARNESS_TICKET_ID not in first_ids

    second = node(_failed(first, "criterion E2-AC01: scenario has no then step"),
                  config_for("r", step=5))
    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2"]
    state: Any = {"artifacts": [*first["artifacts"], *second["artifacts"]]}
    live = {t.id: t for t in read_tickets(state, cas.resolve)}
    assert live[CONTRACTS_TICKET_ID].files == ("notes/contracts.py",)
    assert all(CONTRACTS_TICKET_ID in t.depends_on for tid, t in live.items()
               if tid != CONTRACTS_TICKET_ID)
