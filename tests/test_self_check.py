"""The agent's in-session gate check (foundry.gates.self_check) and the proxy-agent
loop's ``check`` action, done gating and check-budget escalation.

The real-gate tests build a tiny integrated tree from state (as the wave check does)
and run the check on a session's changes; the loop tests fake the gate run.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import foundry.gates.self_check as self_check
from foundry.agents._common import emit_artifact
from foundry.agents.implementation.tickets import CHECK_RULE, verify_rules
from foundry.execution.adapter_contract import TaskPacket
from foundry.execution.adapters.proxy_agent import (
    _EXHAUSTED_IDLE,
    CHECK_EXHAUSTED,
    CHECK_OVERRIDDEN,
    CHECK_RED_LAST_TURN,
    CHECK_UNVERIFIED,
    CHECK_UPSTREAM,
    GREEN_AUTO_DONE,
    PENDING_GREEN_DONE,
    STALLED,
    WRITE_GREEN_DONE,
    ProxyAgentAdapter,
)
from foundry.execution.adapters.tiered import (
    ALREADY_GREEN_TRIAGE,
    CHECK_SCORE_TRIAGE,
    DIGEST_TRIAGE,
    UPSTREAM_DEFECT_TRIAGE,
    TieredAdapter,
    session_stop_reason,
)
from foundry.execution.sandbox import Sandbox
from foundry.gates import sandbox as gate_sandbox
from foundry.gates.pydeps import PythonEnvCache
from foundry.gates.sandbox import ToolchainCache
from foundry.gates.self_check import (
    CheckBase,
    CheckSpec,
    GateOutcome,
    SelfCheckSettings,
    SessionCheck,
    check_base,
    check_scope,
)
from foundry.graph.gate_wiring import CRITERION_KIND, FEATURE_DIFF_KIND, TICKET_KIND
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import (
    FeatureDiff,
    Specialty,
    StackDecision,
    Ticket,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq


def _seq(node: str, local: int = 0) -> Seq:
    return Seq(run_id="run-1", emission_epoch=1, superstep_index=0, node_id=node, local=local)


class _Rt:
    def __init__(self) -> None:
        self.python_envs = PythonEnvCache()
        self.toolchain_cache = ToolchainCache()


def _criterion(cid: str, ticket: str, item: str = "milk") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text="Given … When … Then …",
        ticket_id=ticket,
        oracle_hint=scenario_ref(ticket, cid),
        seq=_seq("planner"),
        given=("an empty list",),
        when=(f'I add "{item}"',),
        then=("the list has 1 item",),
    )


def _state(
    built: Mapping[str, str],
    criteria: tuple[AcceptanceCriterion, ...] = (),
    stack: StackDecision | None = None,
    tickets: tuple[Ticket, ...] = (),
) -> tuple[dict[str, Any], Any, Any]:
    """A state whose integrated tree is ``built`` (one earlier ticket's diff) plus
    the criteria's trusted feature files; ``tickets`` are the plan's."""
    artifacts: list[Artifact] = []
    by_uri: dict[str, Any] = {}
    for t in tickets:
        h = emit_artifact(t, TICKET_KIND, "planner", t.seq)
        artifacts.append(h)
        by_uri[h.uri] = t
    for c in criteria:
        h = emit_artifact(c, CRITERION_KIND, "planner", c.seq)
        artifacts.append(h)
        by_uri[h.uri] = c
    if built:
        diff = FeatureDiff(
            diff_uri="cas://sha256/d0",
            task_id="T0",
            branch="task/T0",
            base_tree_sha="base",
            seq=_seq("junior_engineer"),
        )
        h = Artifact(
            kind=FEATURE_DIFF_KIND,
            uri="cas://fd/T0",
            produced_by="junior_engineer",
            task_id="T0",
            seq=diff.seq,
        )
        artifacts.append(h)
        by_uri[h.uri] = diff
    body = json.dumps({"kind": "full-file-snapshot", "files": dict(built)}).encode()
    state: dict[str, Any] = {"artifacts": artifacts}
    if stack is not None:
        state["stack"] = stack
    return state, (lambda a: by_uri[a.uri]), (lambda _u: body)


@contextlib.contextmanager
def _spec(
    built: Mapping[str, str],
    criteria: tuple[AcceptanceCriterion, ...] = (),
    *,
    own: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    stack: StackDecision | None = None,
    tickets: tuple[Ticket, ...] = (),
) -> Iterator[CheckSpec]:
    state, resolve, blob = _state(built, criteria, stack, tickets)
    with check_base(
        state,
        resolve=resolve,
        resolve_blob=blob,
        rt=_Rt(),  # type: ignore[arg-type]
        settings=SelfCheckSettings(),
    ) as base:
        yield CheckSpec(
            base=base,
            scope=frozenset(scenario_ref(c.ticket_id or "", c.id) for c in criteria),
            own_refs=frozenset(own),
            own_files=files,
        )


def _steps(feature: str, item: str = "milk", ok: bool = True, extra: str = "") -> str:
    return (
        f"{extra}from pytest_bdd import given, parsers, scenarios, then, when\n\n"
        f'scenarios("../features/{feature}")\n\n\n'
        '@given("an empty list", target_fixture="items")\n'
        "def _empty():\n    return []\n\n\n"
        f"@when(parsers.parse('I add \"{{x}}\"'))\n"
        "def _add(items, x):\n    items.append(x)\n\n\n"
        '@then(parsers.parse("the list has {n:d} item"))\n'
        "def _count(items, n):\n"
        f"    assert {'len(items) == n' if ok else 'False'}\n"
    )


def _sandbox_passes(
    root: Path, files: Mapping[str, str], cmd: list[str], env: dict[str, str] | None = None
) -> None:
    """The session's own run of ``cmd`` on ``files`` passes in its sandbox."""
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- the check is the gate --------------------------------------------------------


def test_check_fails_an_import_only_the_sandbox_has(tmp_path: Path) -> None:
    # The session pip-installed tinydep into its sandbox, so its own run passes; the
    # gates install only the declared manifest.
    site = tmp_path / "sandbox-site"
    (site / "tinydep").mkdir(parents=True)
    (site / "tinydep" / "__init__.py").write_text("VALUE = 42\n")
    built = {"requirements.txt": "pytest\n"}
    mine = {"app.py": "import tinydep\n\nVALUE = tinydep.VALUE\n"}
    _sandbox_passes(
        tmp_path / "wt",
        {**built, **mine},
        [sys.executable, "-c", "import app"],
        env={"PYTHONPATH": str(site)},
    )
    with _spec(built, files=("app.py",)) as spec:
        session = SessionCheck(spec)
        session.opening()
        verdict = session.check(mine, ())
    assert verdict.red
    assert any("tinydep" in line for line in verdict.yours), verdict.yours


def test_check_runs_the_trusted_feature_not_the_sessions_copy(tmp_path: Path) -> None:
    # The session rewrote the gate-owned feature to its own step text and bound it:
    # its run passes; the gate writes the trusted feature back and executes that.
    c = _criterion("AC-1", "T1")
    own_feature = c.model_copy(update={"when": ('I put "milk"',)})
    from foundry.graph.gate_wiring import render_feature

    steps = _steps("T1.feature").replace("I add", "I put")
    mine = {"tests/test_t1.py": steps, "features/T1.feature": render_feature("T1", [own_feature])}
    _sandbox_passes(
        tmp_path / "wt",
        mine,
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_t1.py"],
    )
    ref = scenario_ref("T1", "AC-1")
    with _spec({}, (c,), own=(ref,), files=("tests/test_t1.py",)) as spec:
        session = SessionCheck(spec)
        session.opening()
        verdict = session.check(mine, ())
        assert verdict.red and any(ref in line for line in verdict.yours), verdict
        good = session.check({"tests/test_t1.py": _steps("T1.feature")}, ())
    assert not good.red and good.green, good


@pytest.mark.skipif(gate_sandbox.sandbox_backend() is None, reason="no gate sandbox on this host")
def test_check_runs_offline_as_the_gate_does(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)  # the real confined sandbox
    c = _criterion("AC-1", "T1")
    ref = scenario_ref("T1", "AC-1")
    online = "import socket\n\nsocket.create_connection(('1.1.1.1', 53), timeout=3).close()\n\n"
    with _spec({}, (c,), own=(ref,), files=("tests/test_t1.py",)) as spec:
        verdict = SessionCheck(spec).check(
            {"tests/test_t1.py": _steps("T1.feature", extra=online)}, ()
        )
    assert verdict.red and any(ref in line for line in verdict.yours), verdict


def test_the_opening_check_splits_pre_existing_failures_from_the_sessions_own() -> None:
    earlier = _criterion("AC-0", "T0")
    mine = _criterion("AC-1", "T1")
    built = {"tests/test_t0.py": _steps("T0.feature", ok=False)}
    own = scenario_ref("T1", "AC-1")
    with _spec(built, (earlier, mine), own=(own,), files=("tests/test_t1.py",)) as spec:
        session = SessionCheck(spec)
        opening = session.opening()
        done = session.check({"tests/test_t1.py": _steps("T1.feature")}, ())
    theirs = scenario_ref("T0", "AC-0")
    assert any(own in line for line in opening.yours), opening
    assert not any(theirs in line for line in opening.yours), opening
    assert any(theirs in line for line in opening.pre_existing), opening
    # Its scenario passes now; the earlier ticket's failure is still not its own.
    assert not done.red and any(theirs in line for line in done.pre_existing), done
    # A local gate run is seconds, not minutes (pytest-bdd on a two-scenario tree).
    assert opening.seconds < 60 and done.seconds < 60, (opening.seconds, done.seconds)
    text = self_check.render(opening, left=session.left, opening=True)
    assert "PRE-EXISTING" in text and "not yours unless your files cause them" in text


def test_a_failure_the_session_causes_in_an_earlier_ticket_is_its_own() -> None:
    earlier = _criterion("AC-0", "T0")
    built = {
        "tests/test_t0.py": _steps("T0.feature", extra="import app\n\n"),
        "app.py": "VALUE = 1\n",
    }
    with _spec(built, (earlier,), files=("app.py",)) as spec:
        session = SessionCheck(spec)
        assert not session.opening().red
        broke = session.check({"app.py": "VALUE = (\n"}, ())
    assert broke.red, broke


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain on this host")
def test_a_go_stack_runs_through_the_same_check() -> None:
    stack = StackDecision(language="go", build="go build ./...", test="go test ./...")
    built = {
        "go.mod": "module example.com/notes\n\ngo 1.21\n",
        "main.go": "package main\n\nfunc main() { println(Greeting()) }\n",
        "greet.go": 'package main\n\nfunc Greeting() string { return "hi" }\n',
    }
    with _spec(built, stack=stack, files=("greet.go",)) as spec:
        session = SessionCheck(spec)
        assert session.opening().green
        broken = session.check(
            {"greet.go": 'package main\n\nfunc Greeting() int { return "hi" }\n'}, ()
        )
        fixed = session.check(
            {"greet.go": 'package main\n\nfunc Greeting() string { return "hello" }\n'}, ()
        )
    assert broken.red and any("greet.go" in line for line in broken.yours), broken
    assert fixed.green, fixed


# ---- the session's budget and cache -----------------------------------------------


def _fake_base(tmp_path: Path, budget: int = 8) -> CheckBase:
    return CheckBase(
        {},
        tmp_path,
        resolve=lambda a: None,
        resolve_blob=lambda u: b"",
        rt=_Rt(),
        settings=SelfCheckSettings(budget=budget),
    )  # type: ignore[arg-type]


def _red_unless(marker: str, calls: list[dict[str, str]]) -> Any:
    """A fake gate run: the build FAILs naming app.py unless app.py holds ``marker``."""

    def _run(
        spec: CheckSpec, files: Mapping[str, str], deleted: Any = ()
    ) -> tuple[GateOutcome, ...]:
        calls.append(dict(files))
        if marker in files.get("app.py", ""):
            return (GateOutcome("build", GateStatus.PASS, ()),)
        return (GateOutcome("build", GateStatus.FAIL, ("app.py:1: error: not done yet",)),)

    return _run


def test_an_unchanged_tree_answers_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(self_check, "run_check", _red_unless("OK", calls))
    spec = CheckSpec(
        base=_fake_base(tmp_path), scope=frozenset(), own_refs=frozenset(), own_files=("app.py",)
    )
    session = SessionCheck(spec)
    session.opening()
    first = session.check({"app.py": "x"}, ())
    assert session.cached({"app.py": "x"}, ()) == first
    # Out-of-scope files and gate inputs never reach the gate copy or the key.
    assert session.cached({"app.py": "x", "other.py": "y", "features/T1.feature": "z"}, ()) == first
    assert session.cached({"app.py": "OK"}, ()) is None
    assert len(calls) == 2 and session.left == 7


# ---- the loop: check action, done gating, escalation ------------------------------


def _packet(turn_budget: int = 12, files: tuple[str, ...] = ("app.py",)) -> TaskPacket:
    return TaskPacket(
        task={
            "id": "t-1",
            "title": "t-1",
            "branch": "task/t-1",
            "criterion_ids": [],
            "files": list(files),
            "rules": ["VERIFY BEFORE YOU FINISH: run verify"],
        },
        worktree_path="/unused",
        turn_budget=turn_budget,
        cost_budget_microusd=1_000_000,
    )


def _scripted(actions: list[dict[str, Any]], seen: list[list[dict[str, Any]]]) -> Any:
    it = iter(actions)

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        assert "check" in {b["properties"]["action"]["const"] for b in schema["anyOf"]}
        seen.append([dict(m) for m in messages])
        act = next(it)
        return json.dumps(act), act

    return _complete


def _observations(seen: list[list[dict[str, Any]]]) -> list[str]:
    return [str(turn[-1]["content"]) for turn in seen[1:]]


# A ticket with a file no test writes: the check never runs by itself.
_UNFINISHED = ("app.py", "notes.md")


def _run_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[dict[str, Any]],
    budget: int = 8,
    turn_budget: int = 12,
    run: Any = None,
    skip_green: bool = False,
    start: Mapping[str, str] | None = None,
    files: tuple[str, ...] = ("app.py",),
    bodies: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[list[dict[str, Any]]], list[dict[str, str]]]:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(self_check, "run_check", run or _red_unless("OK", calls))
    spec = CheckSpec(
        base=_fake_base(tmp_path / "base", budget),
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=("app.py",),
        rules={"VERIFY BEFORE YOU FINISH: run verify": CHECK_RULE},
        skip_green=skip_green,
    )
    seen: list[list[dict[str, Any]]] = []
    src = tmp_path / "src"
    src.mkdir()
    for rel, text in (start or {}).items():
        (src / rel).write_text(text)
    with Sandbox(src) as sb, check_scope(spec):
        result = ProxyAgentAdapter(_scripted(actions, seen)).run(_packet(turn_budget, files), sb)
        if bodies is not None:
            bodies.append(json.loads(sb.get_blob(result.diff_uri)))
    return result, seen, calls


def test_done_is_refused_on_red_and_allowed_on_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, calls = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "done", "summary": "built it"},
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "check"},
            {"action": "done", "summary": "built it"},
        ],
        files=_UNFINISHED,
    )
    opening = str(seen[0][0]["content"])
    assert "GATE CHECK ON THE STARTING TREE" in opening and CHECK_RULE in opening
    obs = _observations(seen)
    assert obs[1].startswith("OBSERVATION:\nERROR: not done — the gates fail on your work")
    assert "app.py:1: error: not done yet" in obs[1]
    assert "CHECK PASSED" in obs[3]
    assert result.turns_used == 5
    # The opening, the check at the refused done, the explicit check; the accepted
    # done reused the explicit check's result for the same tree.
    assert len(calls) == 3
    assert session_stop_reason(result.triage) is None


def test_a_red_done_is_refused_at_most_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "done", "summary": "a"},
            {"action": "done", "summary": "b"},
            {"action": "done", "summary": "c"},
        ],
    )
    obs = _observations(seen)
    assert all("ERROR: not done — the gates fail" in o for o in obs[1:3])
    assert "call done again" in obs[2]
    assert result.turns_used == 4
    assert session_stop_reason(result.triage) is None  # checks were left
    forced = [t.explanation for t in result.triage if t.explanation.startswith(CHECK_OVERRIDDEN)]
    assert len(forced) == 1 and "app.py:1: error: not done yet" in forced[0], result.triage


def test_a_red_done_on_the_last_turn_is_not_called_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No turn was left to refuse it: "accepted after 0 refused done(s)" misstated it."""
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
            {"action": "done", "summary": "a"},
        ],
        turn_budget=3,
    )
    notes = [t.explanation for t in result.triage]
    assert not any(n.startswith(CHECK_OVERRIDDEN) for n in notes), notes
    (red,) = [n for n in notes if n.startswith(CHECK_RED_LAST_TURN)]
    assert "app.py:1: error: not done yet" in red
    assert session_stop_reason(result.triage) is None


def test_the_check_run_at_done_is_the_digests_last_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "done", "summary": "a"},
            {"action": "read_file", "path": "app.py"},
        ],
        turn_budget=3,
        files=_UNFINISHED,
    )
    digest = next(t.explanation for t in result.triage if t.name == DIGEST_TRIAGE)
    assert "last check: check at done: CHECK FAILED" in digest, digest


def test_a_session_red_after_its_last_check_escalates_like_the_turn_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
            {"action": "write_file", "path": "app.py", "content": "x = 2\n"},
            {"action": "check"},
            {"action": "check"},
            {"action": "done", "summary": "a"},
            {"action": "done", "summary": "b"},
            {"action": "done", "summary": "gave up"},
        ],
        budget=2,
    )
    obs = _observations(seen)
    assert "CHECK FAILED" in obs[4] and "cached" in obs[4]  # no budget, same tree
    stop = session_stop_reason(result.triage)
    assert stop is not None and stop.startswith(CHECK_EXHAUSTED), result.triage


def _red_by_count(calls: list[dict[str, str]]) -> Any:
    """A fake gate run: app.py's first line ``n = K`` fails the build with K lines."""

    def _run(
        spec: CheckSpec, files: Mapping[str, str], deleted: Any = ()
    ) -> tuple[GateOutcome, ...]:
        calls.append(dict(files))
        head = files.get("app.py", "n = 1").splitlines()[0]
        n = int(head.split("=")[1].split("#")[0])
        lines = tuple(f"app.py:{i}: error: wrong" for i in range(1, n + 1))
        return (GateOutcome("build", GateStatus.FAIL, lines),)

    return _run


def _score(result: Any) -> str | None:
    return next((t.explanation for t in result.triage if t.name == CHECK_SCORE_TRIAGE), None)


def test_checks_that_stop_improving_end_the_session_on_its_best_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions: list[dict[str, Any]] = []
    for text in ("n = 3  # best\n", "n = 5\n", "n = 4\n", "n = 3  # tie\n"):
        actions += [{"action": "write_file", "path": "app.py", "content": text}]
        actions += [{"action": "check"}]
    actions.append({"action": "done", "summary": "never reached"})
    bodies: list[dict[str, Any]] = []
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        actions,
        turn_budget=40,
        run=_red_by_count([]),
        files=_UNFINISHED,
        bodies=bodies,
    )
    assert result.turns_used == 8
    stop = session_stop_reason(result.triage)
    assert stop is not None and stop.startswith(f"stopped early: {STALLED}"), result.triage
    assert _score(result) == "3"
    assert bodies[0]["files"]["app.py"] == "n = 3  # best\n"


def test_turns_past_the_last_check_without_done_end_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions: list[dict[str, Any]] = [
        {"action": "write_file", "path": "app.py", "content": "n = 2\n"},
        {"action": "check"},
    ]
    actions += [
        {"action": "write_file", "path": "app.py", "content": f"n = 2  # blind {i}\n"}
        for i in range(30)
    ]
    bodies: list[dict[str, Any]] = []
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        actions,
        budget=1,
        turn_budget=40,
        run=_red_by_count([]),
        files=_UNFINISHED,
        bodies=bodies,
    )
    assert result.turns_used == 2 + _EXHAUSTED_IDLE
    stop = session_stop_reason(result.triage)
    assert stop is not None and stop.startswith(f"stopped early: {STALLED}"), result.triage
    assert _score(result) == "2"  # the checked tree is the one recorded
    assert bodies[0]["files"]["app.py"] == "n = 2\n"


def test_checks_that_keep_improving_do_not_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions: list[dict[str, Any]] = []
    for n in (9, 10, 8, 9, 7, 8, 6):
        actions += [{"action": "write_file", "path": "app.py", "content": f"n = {n}\n"}]
        actions += [{"action": "check"}]
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        actions,
        budget=20,
        turn_budget=len(actions),
        run=_red_by_count([]),
        files=_UNFINISHED,
    )
    assert result.turns_used == len(actions)
    assert not any(STALLED in t.explanation for t in result.triage), result.triage


def test_an_edit_after_the_last_check_is_not_reported_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
            {"action": "write_file", "path": "app.py", "content": "x = 2  # OK\n"},
            {"action": "check"},
            {"action": "done", "summary": "fixed, unchecked"},
        ],
        budget=1,
    )
    assert "ERROR: no checks left" in _observations(seen)[3]
    # No check left to judge the final tree: done is accepted, and nothing says red.
    assert session_stop_reason(result.triage) is None, result.triage
    assert any(t.explanation.startswith(CHECK_UNVERIFIED) for t in result.triage)


def test_a_refusal_with_no_check_left_does_not_ask_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
            {"action": "done", "summary": "a"},
            {"action": "done", "summary": "b"},
            {"action": "done", "summary": "c"},
        ],
        budget=1,
    )
    refusal = _observations(seen)[2]
    assert "ERROR: not done" in refusal and "No checks are left" in refusal
    assert "call check" not in refusal


_ERRORED = (GateOutcome("build", GateStatus.ERROR, ("pip: connection reset",)),)


def _errors_after_opening(calls: list[Any]) -> Any:
    def _run(spec: CheckSpec, files: Mapping[str, str], deleted: Any = ()) -> Any:
        calls.append(dict(files))
        return (GateOutcome("build", GateStatus.PASS, ()),) if not files else _ERRORED

    return _run


def test_a_check_no_gate_could_judge_is_not_cached_and_spends_only_past_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(self_check, "run_check", _errors_after_opening(calls))
    session = SessionCheck(
        CheckSpec(
            base=_fake_base(tmp_path, budget=3),
            scope=frozenset(),
            own_refs=frozenset(),
            own_files=("app.py",),
        )
    )
    session.opening()
    tree = {"app.py": "a"}
    assert session.check(tree, ()).errors
    assert session.cached(tree, ()) is None and session.unjudged(tree, ())
    assert session.left == 3
    session.check(tree, ())
    assert session.left == 3
    session.check(tree, ())  # past the free unjudged runs: an always-broken gate is bounded
    assert session.left == 2 and len(calls) == 4


def test_an_unjudged_final_tree_is_unverified_and_done_keeps_its_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "done", "summary": "built it", "triage": ["the cache is untested"]},
        ],
        run=_errors_after_opening(calls),
    )
    notes = [t.explanation for t in result.triage]
    assert notes[:2] == ["built it", "the cache is untested"], notes
    assert any(n.startswith(CHECK_UNVERIFIED) and "could not judge" in n for n in notes), notes
    assert session_stop_reason(result.triage) is None


def test_a_session_out_of_turns_with_checks_left_says_why_it_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
        ],
        turn_budget=1,
        files=_UNFINISHED,
    )
    [note] = [t.explanation for t in result.triage if t.explanation.startswith(CHECK_UNVERIFIED)]
    assert "ended without checking" in note and "no check" not in note, note


def test_the_session_end_walks_the_final_tree_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.execution.adapters.proxy_agent as proxy_agent

    walks: list[Path] = []
    real = proxy_agent.walk_worktree

    def _walk(root: Path) -> Any:
        walks.append(root)
        return real(root)

    monkeypatch.setattr(proxy_agent, "walk_worktree", _walk)
    _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "check"},
            {"action": "done", "summary": "built it"},
        ],
        budget=1,
    )
    # The start, the check, the done's check lookup, and one walk at the end.
    assert len(walks) == 4, walks


def test_check_without_a_gate_context_is_unavailable(tmp_path: Path) -> None:
    seen: list[list[dict[str, Any]]] = []
    actions = iter([{"action": "check"}, {"action": "done", "summary": "x"}])

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        assert "check" not in {b["properties"]["action"]["const"] for b in schema["anyOf"]}
        seen.append(list(messages))
        act = next(actions)
        return json.dumps(act), act

    src = tmp_path / "src"
    src.mkdir()
    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(_complete).run(_packet(), sb)
    assert "check is unavailable" in str(seen[1][-1]["content"])
    assert "GATE CHECK" not in str(seen[0][0]["content"])
    assert result.turns_used == 2


def test_the_check_rule_replaces_the_verify_rule_only_when_asked() -> None:
    assert CHECK_RULE not in verify_rules(None)
    assert verify_rules(None, check=True)[0] == CHECK_RULE


def test_concurrent_checkouts_of_one_manifest_keep_their_own_readiness(tmp_path: Path) -> None:
    cache = PythonEnvCache()
    first = cache.checkout("d" * 64, ("flask",))
    first.ready = True
    second = cache.checkout("d" * 64, ("flask",))
    assert first.ready and not second.ready
    assert first.venv == second.venv
    cache.close()


# ---- review round 2: baselines, attribution, unbuilt files, sharing ---------------


def _fake_run(outcomes: list[tuple[GateOutcome, ...]], calls: list[Any]) -> Any:
    it = iter(outcomes)

    def _run(spec: CheckSpec, files: Mapping[str, str], deleted: Any = ()) -> Any:
        calls.append(dict(files))
        return next(it)

    return _run


def _build(status: GateStatus, *lines: str) -> GateOutcome:
    return GateOutcome(
        "build",
        status,
        ("build command exited 1; last lines:", *lines) if status is GateStatus.FAIL else lines,
    )


def test_an_opening_error_takes_the_first_judging_result_as_its_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        self_check,
        "run_check",
        _fake_run(
            [
                (GateOutcome("build", GateStatus.ERROR, ("pip: connection reset",)),),
                (_build(GateStatus.FAIL, "other.py:1: error: old"),),
                (_build(GateStatus.FAIL, "other.py:1: error: old", "other.py:9: error: new"),),
            ],
            calls,
        ),
    )
    spec = CheckSpec(
        base=_fake_base(tmp_path), scope=frozenset(), own_refs=frozenset(), own_files=("app.py",)
    )
    session = SessionCheck(spec)
    opening = session.opening()
    assert opening.errors and not opening.red
    first = session.check({"app.py": "a"}, ())
    assert not first.red and first.pre_existing  # no baseline yet: its own baseline
    second = session.check({"app.py": "b"}, ())
    assert second.red and second.yours[-1] == "other.py:9: error: new", second


def test_a_build_error_new_since_the_opening_is_the_sessions_even_in_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        self_check,
        "run_check",
        _fake_run(
            [
                (_build(GateStatus.FAIL, "other.py:1: error: old"),),
                (
                    _build(
                        GateStatus.FAIL,
                        "other.py:1: error: old",
                        "caller.py:5: error: too many arguments",
                    ),
                ),
                (_build(GateStatus.FAIL, "other.py:1: error: old"),),
            ],
            calls,
        ),
    )
    spec = CheckSpec(
        base=_fake_base(tmp_path), scope=frozenset(), own_refs=frozenset(), own_files=("app.py",)
    )
    session = SessionCheck(spec)
    assert not session.opening().red
    broke = session.check({"app.py": "a"}, ())
    assert broke.red and "caller.py:5: error: too many arguments" in broke.yours
    assert "other.py:1: error: old" not in broke.yours
    assert not session.check({"app.py": "b"}, ()).red


def test_another_tickets_module_that_stops_collecting_points_at_the_changed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foundry.gates.test import UNCOLLECTED_FIX

    ref = "features/T1.feature::T1-AC-1"
    green = GateOutcome("test", GateStatus.PASS, (), {ref: "passed"})
    broke = GateOutcome(
        "test",
        GateStatus.FAIL,
        (
            f"BDD criterion T1-AC-1: scenario {ref} was not executed — "
            "tests/step_defs/test_t1.py binds it, but pytest failed to import that module "
            "(`ERROR collecting tests/step_defs/test_t1.py` in the output), so none of its "
            f"scenarios ran: {UNCOLLECTED_FIX}",
        ),
        {ref: "missing"},
    )
    calls: list[Any] = []
    monkeypatch.setattr(self_check, "run_check", _fake_run([(green,), (broke,)], calls))
    spec = CheckSpec(
        base=_fake_base(tmp_path),
        scope=frozenset({ref}),
        own_refs=frozenset(),
        own_files=("pyproject.toml", "app/cli.py"),
    )
    session = SessionCheck(spec)
    assert not session.opening().red
    v = session.check({"pyproject.toml": "[tool.pytest.ini_options]\n", "app/cli.py": "x"}, ())
    text = "\n".join(v.yours)
    assert v.red and UNCOLLECTED_FIX not in text, text
    assert "you changed (pyproject.toml)" in text and "not tests/step_defs/test_t1.py" in text
    assert "the fix is not in tests/step_defs/test_t1.py" in text


def test_a_foreign_module_failing_at_the_opening_gets_no_config_note() -> None:
    from foundry.gates.test import UNCOLLECTED_FIX

    line = (
        "BDD criterion T1-AC-1: scenario f::T1-AC-1 was not executed — tests/test_t1.py "
        f"binds it, but pytest failed to import that module: {UNCOLLECTED_FIX}"
    )
    red = GateOutcome("test", GateStatus.FAIL, (line,), {"f::T1-AC-1": "missing"})
    spec = CheckSpec(
        base=None,  # type: ignore[arg-type]
        scope=frozenset({"f::T1-AC-1"}),
        own_refs=frozenset(),
        own_files=("conftest.py",),
    )
    assert self_check._foreign_collection(spec, red, red, ("conftest.py",)) == (line,)
    # Nothing changed and nothing owned configures the harness: the advice still moves.
    spec2 = CheckSpec(
        base=None,  # type: ignore[arg-type]
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=("app.py",),
    )
    green = GateOutcome("test", GateStatus.PASS, ())
    out = self_check._foreign_collection(spec2, red, green, ())
    assert UNCOLLECTED_FIX not in "\n".join(out) and "your own changes" in out[0]


def test_sessions_with_one_scope_share_one_opening_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(self_check, "run_check", _red_unless("OK", calls))
    base = _fake_base(tmp_path)
    for files in (("app.py",), ("b.py",)):
        SessionCheck(
            CheckSpec(base=base, scope=frozenset(), own_refs=frozenset(), own_files=files)
        ).opening()
    SessionCheck(CheckSpec(base=base, scope=frozenset({"x"}), own_refs=frozenset())).opening()
    assert len(calls) == 2


def test_report_paths_are_stable_across_processes() -> None:
    line = (
        'File "/private/var/folders/x/T/foundry-gate-pyenvs-ab12cd34/0a/venv/lib/x.py"'
        " and /tmp/foundry-check-base-zz99yy88/tree/app.py"
    )
    out = self_check._stable([line])[0]
    assert "ab12cd34" not in out and "zz99yy88" not in out and "/private/var" not in out
    assert "$TMP/foundry-gate-pyenvs-/0a/venv/lib/x.py" in out


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain on this host")
def test_a_build_error_on_a_sibling_tickets_unbuilt_package_does_not_block() -> None:
    stack = StackDecision(language="go", build="go build ./...", test="go test ./...")
    built = {
        "go.mod": "module example.com/notes\n\ngo 1.21\n",
        "main.go": "package main\n\nfunc main() { run() }\n",
    }
    tickets = (
        Ticket(
            id="T-store",
            title="store",
            specialty=Specialty.BACKEND,
            files=("store/store.go",),
            seq=_seq("planner", 1),
        ),
        Ticket(
            id="T-cmd",
            title="cmd",
            specialty=Specialty.BACKEND,
            files=("cmd.go",),
            seq=_seq("planner", 2),
        ),
    )
    cmd = 'package main\n\nimport "example.com/notes/store"\n\nfunc run() { store.Open() }\n'
    with _spec(built, stack=stack, files=("cmd.go",), tickets=tickets) as spec:
        session = SessionCheck(spec)
        session.opening()
        waiting = session.check({"cmd.go": cmd}, ())
        typo = session.check({"cmd.go": 'package main\n\nfunc run() int { return "x" }\n'}, ())
    assert not waiting.red, waiting
    assert any("waits on a planned file" in line for line in waiting.pre_existing), waiting
    assert typo.red and any("cmd.go" in line for line in typo.yours), typo


def test_the_opening_check_runs_before_the_local_slot_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    calls: list[Any] = []
    fake = _red_unless("", calls)  # always green

    def _run(*a: Any, **k: Any) -> Any:
        order.append("check")
        return fake(*a, **k)

    monkeypatch.setattr(self_check, "run_check", _run)

    @contextlib.contextmanager
    def _slot() -> Iterator[None]:
        order.append("slot")
        yield

    spec = CheckSpec(
        base=_fake_base(tmp_path / "base"),
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=("app.py",),
    )
    seen: list[list[dict[str, Any]]] = []
    local = ProxyAgentAdapter(_scripted([{"action": "done", "summary": "x"}], seen))
    tiered = TieredAdapter(cloud=local, local=local, local_tiers={"T3"}, local_slot=_slot)
    src = tmp_path / "src"
    src.mkdir()
    packet = _packet().model_copy(update={"task": {**_packet().task, "tier": "T3"}})
    with Sandbox(src) as sb, check_scope(spec):
        tiered.run(packet, sb)
    assert order[:2] == ["check", "slot"]
    assert "GATE CHECK ON THE STARTING TREE" in str(seen[0][0]["content"])


def test_the_check_can_be_kept_to_local_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_SELF_CHECK", "local")
    assert not self_check.wanted()
    with self_check.local_session():
        assert self_check.wanted()
    monkeypatch.setenv("FOUNDRY_SELF_CHECK", "0")
    with self_check.local_session():
        assert not self_check.wanted()


def test_the_runs_self_check_mode_wins_over_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_SELF_CHECK", "0")
    assert not SelfCheckSettings().enabled()
    assert SelfCheckSettings(mode="local").enabled()
    assert not self_check.wanted("local") and self_check.wanted("on")
    monkeypatch.delenv("FOUNDRY_SELF_CHECK")
    assert not SelfCheckSettings(mode="off").enabled()
    assert not self_check.wanted("off")


def test_concurrent_builds_reuse_a_failed_install_and_never_delete_a_held_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.gates.pydeps as pydeps

    installs: list[str] = []
    monkeypatch.setattr(pydeps, "build_python_env", lambda venv: venv.mkdir(parents=True))

    def _pip(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        installs.append("pip")
        return 1, "", "ERROR: No matching distribution found for nosuchpkg-xyz\n"

    monkeypatch.setattr(pydeps, "ground_untrusted", _pip)
    cache = PythonEnvCache()
    wd = tmp_path / "wd"
    wd.mkdir()
    first = cache.checkout("d" * 64, ("nosuchpkg-xyz",))
    second = cache.checkout("d" * 64, ("nosuchpkg-xyz",))
    assert first.install(wd, timeout_s=5)[0] == 1
    (first.venv / "held").write_text("in use")
    assert second.install(wd, timeout_s=5)[0] == 1
    assert installs == ["pip"] and (first.venv / "held").exists()
    first.release()
    second.release()
    # Nobody holds it any more: the next build of the manifest retries.
    third = cache.checkout("d" * 64, ("nosuchpkg-xyz",))
    third.install(wd, timeout_s=5)
    assert installs == ["pip", "pip"]
    cache.close()


def test_a_fixer_sent_for_smoke_checks_the_smoke_command() -> None:
    """The shop run: smoke failed on a missing smoke.py, and the fixer's check (build
    + scenarios only) never showed it."""
    from foundry.gates.profiles import PROFILES

    state, resolve, blob = _state({"app.py": "X = 1\n"})
    settings = SelfCheckSettings(profile=PROFILES["python-app"])
    with check_base(
        state,
        resolve=resolve,
        resolve_blob=blob,
        rt=_Rt(),  # type: ignore[arg-type]
        settings=settings,
    ) as base:
        quiet = CheckSpec(base=base, scope=frozenset(), own_refs=frozenset())
        assert "smoke" not in SessionCheck(quiet).opening().checked
        spec = CheckSpec(
            base=base, scope=frozenset(), own_refs=frozenset(), smoke=True, own_smoke=True
        )
        session = SessionCheck(spec)
        opening = session.opening()
        fixed = session.check({"smoke.py": "import app\n"}, ())
    assert "smoke" in opening.checked and opening.red
    assert opening.yours[0] == "[smoke gate FAILED]"
    assert any("smoke.py" in line for line in opening.yours), opening.yours
    assert fixed.green, fixed


# ---- a green session: skipped, or stopped once green ------------------------------


def _names(result: Any) -> list[str]:
    return [t.name for t in result.triage]


def test_a_green_opening_on_built_files_skips_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [],
        run=_red_unless("", calls),  # always green
        skip_green=True,
        start={"app.py": "def f() -> int:\n    return 1\n"},
    )
    assert seen == [] and result.turns_used == 0
    assert ALREADY_GREEN_TRIAGE in _names(result)
    assert session_stop_reason(result.triage) is None
    assert len(calls) == 1  # the opening check only


@pytest.mark.parametrize(
    ("skip_green", "start"),
    [
        (False, {"app.py": "X = 1\n"}),  # the spec does not allow it (a retry)
        (True, {}),  # the ticket's file is not built yet
        (True, {"app.py": "def f() -> int:\n    raise NotImplementedError\n"}),  # a stub
    ],
)
def test_a_green_opening_still_runs_a_session_with_work_left(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skip_green: bool,
    start: dict[str, str],
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [{"action": "done", "summary": "x"}],
        run=_red_unless("", []),
        skip_green=skip_green,
        start=start,
    )
    assert len(seen) == 1 and ALREADY_GREEN_TRIAGE not in _names(result)


def test_a_green_opening_with_a_file_to_write_ends_once_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "X = 1\n"},
            {"action": "read_file", "path": "app.py"},  # never asked for
        ],
        run=_red_unless("", calls),  # always green
        skip_green=True,
    )
    assert len(seen) == 1 and result.turns_used == 1
    assert "this ticket is done once app.py exist" in seen[0][0]["content"]
    assert PENDING_GREEN_DONE in [t.explanation for t in result.triage]
    assert session_stop_reason(result.triage) is None
    assert len(calls) == 2  # the opening check, then the one after the write
    assert ALREADY_GREEN_TRIAGE not in _names(result)


def test_a_green_opening_whose_written_file_turns_it_red_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _run(spec: CheckSpec, files: Mapping[str, str], deleted: Any = ()) -> Any:
        if "app.py" in files and "OK" not in files["app.py"]:
            return (GateOutcome("build", GateStatus.FAIL, ("app.py:1: error: bad",)),)
        return (GateOutcome("build", GateStatus.PASS, ()),)

    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "X = 1\n"},
            {"action": "write_file", "path": "app.py", "content": "X = 1  # OK\n"},
            {"action": "check"},
            {"action": "done", "summary": "fixed"},
        ],
        run=_run,
        skip_green=True,
    )
    assert len(seen) == 2  # the fixing write's own check passed and ended it
    assert (
        "run by itself" in seen[1][-1]["content"]
        and "app.py:1: error: bad" in (seen[1][-1]["content"])
    )
    notes = [t.explanation for t in result.triage]
    assert PENDING_GREEN_DONE not in notes and WRITE_GREEN_DONE in notes


def test_a_write_completing_the_ticket_runs_the_check_and_ends_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature ticket that goes green mid-session ends without a done turn."""
    result, seen, calls = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "done", "summary": "never asked"},
        ],
    )
    obs = _observations(seen)
    assert "CHECK (run by itself" in obs[0] and "app.py:1: error: not done yet" in obs[0]
    assert len(seen) == 2 and result.turns_used == 2
    notes = [t.explanation for t in result.triage]
    assert "wrote app.py; the gate check run after the last write passed" in notes, notes
    assert WRITE_GREEN_DONE in notes
    assert session_stop_reason(result.triage) is None
    assert DIGEST_TRIAGE not in _names(result)
    assert len(calls) == 3  # the opening, then one per completing write


def test_an_edit_completing_the_ticket_runs_the_check_and_ends_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit = {"action": "edit_file", "path": "app.py"}
    result, seen, calls = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {**edit, "edits": [{"old": "x = 1\n", "new": "x = 2\n"}]},
            {**edit, "edits": [{"old": "x = 2\n", "new": "x = 2  # OK\n"}]},
            {"action": "done", "summary": "never asked"},
        ],
        start={"app.py": "x = 1\n"},
    )
    obs = _observations(seen)
    assert obs[0].startswith("OBSERVATION:\nedited app.py")
    assert "CHECK (run by itself" in obs[0] and "app.py:1: error: not done yet" in obs[0]
    assert len(seen) == 2 and result.turns_used == 2
    assert WRITE_GREEN_DONE in [t.explanation for t in result.triage]
    assert len(calls) == 3  # the opening, then one per edit


def test_no_check_runs_by_itself_while_a_ticket_file_is_unbuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, calls = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "write_file", "path": "notes.md", "content": "# notes\n"},
            {"action": "done", "summary": "never asked"},
        ],
        files=_UNFINISHED,
    )
    obs = _observations(seen)
    assert "run by itself" not in obs[0]
    assert len(seen) == 2 and len(calls) == 2  # the opening, then once notes.md exists
    assert WRITE_GREEN_DONE in [t.explanation for t in result.triage]


def test_a_native_turn_writing_several_files_is_checked_once_after_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(self_check, "run_check", _red_unless("OK", calls))
    spec = CheckSpec(
        base=_fake_base(tmp_path / "base"),
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=_UNFINISHED,
    )
    turns: list[list[dict[str, Any]]] = []
    script = iter(
        [
            [
                {"id": "a", "name": "write_file", "input": {"path": "app.py", "content": "x\n"}},
                {"id": "b", "name": "write_file", "input": {"path": "notes.md", "content": "n"}},
                {"id": "c", "name": "read_file", "input": {"path": "notes.md"}},
            ],
            [{"id": "d", "name": "write_file", "input": {"path": "app.py", "content": "OK\n"}}],
        ]
    )

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        turns.append([dict(m) for m in messages])
        return "", next(script)

    src = tmp_path / "src"
    src.mkdir()
    with Sandbox(src) as sb, check_scope(spec):
        result = ProxyAgentAdapter(_scripted([], []), tool_complete_fn=_tools).run(
            _packet(files=_UNFINISHED), sb
        )
    first = turns[1][-1]["content"]
    assert [r["tool_use_id"] for r in first] == ["a", "b", "c"]
    assert "run by itself" not in first[0]["content"] + first[1]["content"]
    assert "run by itself" in first[2]["content"]  # once, after the turn's last call
    assert len(turns) == 2 and len(calls) == 3
    assert WRITE_GREEN_DONE in [t.explanation for t in result.triage]


def test_a_red_done_naming_an_upstream_defect_is_accepted_without_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    defect = "upstream-defect: web/routes.py: DELETE is a 405; repro: client.delete('/n/1')"
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "done", "summary": "wired it", "triage": [defect]},
        ],
    )
    assert len(seen) == 2 and result.turns_used == 2
    notes = [t.explanation for t in result.triage]
    assert any(n.startswith(CHECK_UPSTREAM) for n in notes), notes
    assert UPSTREAM_DEFECT_TRIAGE in _names(result)


def test_the_explicit_check_still_runs_on_a_complete_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, seen, calls = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
            {"action": "done", "summary": "a"},
            {"action": "done", "summary": "b"},
            {"action": "done", "summary": "c"},
        ],
    )
    obs = _observations(seen)
    assert "CHECK (run by itself" in obs[0]
    assert "CHECK FAILED" in obs[1] and "app.py:1: error: not done yet" in obs[1]
    assert len(calls) == 2  # the explicit check reused the automatic one's verdict


def test_a_skipped_local_session_never_takes_the_local_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(self_check, "run_check", _red_unless("", []))
    taken: list[str] = []

    @contextlib.contextmanager
    def _slot() -> Iterator[None]:
        taken.append("slot")
        yield

    spec = CheckSpec(
        base=_fake_base(tmp_path / "base"),
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=("app.py",),
        skip_green=True,
    )
    local = ProxyAgentAdapter(_scripted([], []))
    tiered = TieredAdapter(cloud=local, local=local, local_tiers={"T3"}, local_slot=_slot)
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("X = 1\n")
    packet = _packet().model_copy(update={"task": {**_packet().task, "tier": "T3"}})
    with Sandbox(src) as sb, check_scope(spec):
        result = tiered.run(packet, sb)
    assert taken == [] and ALREADY_GREEN_TRIAGE in _names(result)


def test_after_a_green_check_only_done_or_an_edit_may_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "check"},
            {"action": "run", "cmd": "touch ran-1"},
            {"action": "run", "cmd": "touch ran-2"},
            {"action": "done", "summary": "never asked"},
        ],
        files=_UNFINISHED,
    )
    obs = _observations(seen)
    assert "CHECK PASSED" in obs[1]
    assert obs[2].startswith("OBSERVATION:\nNOT RUN: the gate check passed")
    assert result.turns_used == 4  # the second idle turn ended it as done
    notes = [t.explanation for t in result.triage]
    assert GREEN_AUTO_DONE in notes and session_stop_reason(result.triage) is None
    assert DIGEST_TRIAGE not in _names(result)


def test_an_edit_after_a_green_check_reopens_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1  # OK\n"},
            {"action": "check"},
            {"action": "write_file", "path": "app.py", "content": "x = 2  # OK\n"},
            {"action": "run", "cmd": "echo hi"},
            {"action": "done", "summary": "built it"},
        ],
        files=_UNFINISHED,
    )
    obs = _observations(seen)
    assert obs[3].startswith("OBSERVATION:\nexit 0")
    assert GREEN_AUTO_DONE not in [t.explanation for t in result.triage]


def test_the_last_checks_failing_count_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_loop(
        tmp_path,
        monkeypatch,
        [
            {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
            {"action": "check"},
        ],
        turn_budget=2,
    )
    score = [t.explanation for t in result.triage if t.name == CHECK_SCORE_TRIAGE]
    assert score == ["1"]  # one failing line; the gate header is not counted


def test_an_already_green_ticket_is_built_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foundry.agents.implementation.tickets.adapter import _ScopedAdapter
    from foundry.durability.artifact_store import ArtifactStore
    from tests.test_ticket_scheduler import _runtime

    monkeypatch.setattr(self_check, "run_check", _red_unless("", []))
    spec = CheckSpec(
        base=_fake_base(tmp_path / "base"),
        scope=frozenset(),
        own_refs=frozenset(),
        own_files=("app.py",),
        skip_green=True,
    )
    local = ProxyAgentAdapter(_scripted([], []))
    scoped = _ScopedAdapter(
        _runtime(local, ArtifactStore()), {"t-1": ("app.py",)}, checks={"t-1": spec}
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("X = 1\n")
    with Sandbox(src) as sb:
        result = scoped.run(_packet(), sb)  # no EmptyTicketError: its file is built
    assert ALREADY_GREEN_TRIAGE in _names(result) and scoped.empty == {}
