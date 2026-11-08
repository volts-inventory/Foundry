"""Build control in the Workshop: Stop / Pause / Resume / Retry with… / Delete.

Stop and Pause halt a build at its next checkpoint; Resume continues it from the
durable checkpoint (optionally with a new token cap or fix budget); Delete removes
one finished floor. Every new route sits behind the same sign-in + CSRF guard.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.proxy.token_meter import BuildStopped, TokenCapExceeded, TokenMeter
from webapp import server
from webapp.server import Project
from webapp.store import BuildStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A hermetic Workshop: its own catalog, state dir and output root, no pacing,
    sign-in off, and a live HTTP server on a free port."""
    store = BuildStore(tmp_path / "foundry.db")
    monkeypatch.setattr(server, "_STORE", store)
    monkeypatch.setattr(server, "_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "_OUTPUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(server, "_PROJECTS", {})
    monkeypatch.setattr(server, "_FLOOR", [0])
    monkeypatch.setattr(server, "_PACE", 0)
    monkeypatch.setattr(server, "_BUILD_GATE", None)
    monkeypatch.setattr(server, "_AUTH_TOKEN", None)
    monkeypatch.setenv("FOUNDRY_AUTH", "0")
    monkeypatch.setenv("FOUNDRY_MODEL_BACKEND", "mock")
    monkeypatch.delenv("FOUNDRY_TOKEN_CAP", raising=False)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=srv.server_port, store=store, tmp=tmp_path)
    srv.shutdown()


def _call(
    env: Any, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(
        method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    resp = conn.getresponse()
    out = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, out


def _wait_ended(p: Project, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not (p.ended and p.status == "done") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert p.ended and p.status == "done", "build did not finish in time"


def _new(env: Any, **body: Any) -> Project:
    code, got = _call(env, "POST", "/api/projects", {"request": "build a thing", **body})
    assert code == 200, got
    return server._PROJECTS[got["id"]]


def _record_nodes(monkeypatch, on_start=None) -> list[str]:
    """Record every graph node that starts (via the server's live telemetry hook)."""
    started: list[str] = []
    real = server.live_event_for_node

    def spy(node, scope, phase, result):
        if phase == "start":
            started.append(node)
            if on_start is not None:
                on_start(node)
        return real(node, scope, phase, result)

    monkeypatch.setattr(server, "live_event_for_node", spy)
    return started


def _stream_types(env: Any, pid: str) -> list[str]:
    """Every event a fresh viewer replays, up to the stream's end."""
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=30)
    conn.request("GET", f"/api/stream/{pid}")
    resp = conn.getresponse()
    types: list[str] = []
    while True:
        line = resp.fp.readline().decode()
        if line.startswith("event: end"):
            break
        if line.startswith("data: "):
            types.append(json.loads(line[6:])["type"])
        assert line, "stream closed without an end event"
    conn.close()
    return types


# --- the cancel seam ---------------------------------------------------------- #


def test_meter_cancel_raises_build_stopped_and_cap_is_optional() -> None:
    cancel = threading.Event()
    meter = TokenMeter(None, cancel=cancel)
    meter.charge({"input_tokens": 10**9})
    meter.check()  # no cap: never spent
    cancel.set()
    with pytest.raises(BuildStopped):
        meter.check()
    with pytest.raises(BuildStopped):
        meter.check_cancel()
    # A stop rides the token cap's clean-stop path wherever that is caught.
    assert issubclass(BuildStopped, TokenCapExceeded)


def test_a_stop_is_honoured_before_a_gate_run(tmp_path) -> None:
    """Gates make no model call, so the supervisor checks the stop before each gate."""
    from foundry.cli import run
    from tests.test_cli_smoke import _ERROR_DIFF

    state = str(tmp_path)
    assert run("build a thing", state_dir=state, diff_body=_ERROR_DIFF).resume_from
    stopped = threading.Event()
    stopped.set()
    started: list[str] = []
    with pytest.raises(BuildStopped):  # the resume goes straight to the final gates
        run(
            "build a thing",
            state_dir=state,
            diff_body=_ERROR_DIFF,
            cancel=stopped,
            progress_cb=lambda n, _s, **k: started.append(n) if k["phase"] == "start" else None,
        )
    assert started[-1] == "build_test_gate" and "planner" not in started


# --- stop / pause / resume ----------------------------------------------------- #


def test_stop_mid_build_halts_at_a_checkpoint_and_resume_finishes_without_replanning(
    env,
    monkeypatch,
):
    reached, release = threading.Event(), threading.Event()

    def hold_at_engineering(node: str) -> None:
        if node == "engineering" and not reached.is_set():
            reached.set()
            release.wait(10)  # the build is mid-implementation while we stop it

    started = _record_nodes(monkeypatch, hold_at_engineering)
    p = _new(env)
    assert reached.wait(30)
    code, got = _call(env, "POST", f"/api/projects/{p.id}/stop")
    assert (code, got["kind"]) == (200, "stopped")
    assert _call(env, "POST", f"/api/projects/{p.id}/stop")[0] == 409  # already stopping
    release.set()
    _wait_ended(p)

    assert (p.phase, p.passed) == ("stopped", False)
    assert server._has_durable_checkpoint(p.id)
    halt = [e for e in p.log if e["type"] == "halt"][-1]
    assert halt["phase"] == "stopped"
    assert "stopping" in [e["type"] for e in p.log]

    planned = started.count("planner")
    code, got = _call(env, "POST", f"/api/projects/{p.id}/resume")
    assert code == 200 and got["from_checkpoint"] is True
    _wait_ended(p)
    assert (p.phase, p.passed) == ("passed", True)
    assert started.count("planner") == planned, "a resume must not re-plan"

    # A fresh viewer replays both runs: the stopped run's _end mid-log is skipped.
    types = _stream_types(env, p.id)
    assert types.count("done") == 2
    assert types.index("done") < types.index("resumed") < len(types) - 1


def test_pause_is_a_resumable_stop_labelled_paused(env, monkeypatch):
    reached, release = threading.Event(), threading.Event()

    def hold(node: str) -> None:
        if node == "engineering" and not reached.is_set():
            reached.set()
            release.wait(10)

    _record_nodes(monkeypatch, hold)
    p = _new(env)
    assert reached.wait(30)
    assert _call(env, "POST", f"/api/projects/{p.id}/pause")[0] == 200
    rows = _call(env, "GET", "/api/projects")[1]["projects"]
    assert rows[0]["stopping"] == "paused"
    release.set()
    _wait_ended(p)
    assert p.phase == "paused"
    assert [e for e in p.log if e["type"] == "done"][-1]["phase"] == "paused"
    assert _call(env, "POST", f"/api/projects/{p.id}/resume")[0] == 200
    _wait_ended(p)
    assert p.passed


def test_resume_a_token_capped_build_with_a_higher_cap(env, monkeypatch):
    started = _record_nodes(monkeypatch)
    p = _new(env, token_cap=1)
    _wait_ended(p)
    assert p.phase == "token_cap" and not p.passed
    planned = started.count("planner")

    code, _ = _call(env, "POST", f"/api/projects/{p.id}/resume", {"token_cap": 10**9})
    assert code == 200
    _wait_ended(p)
    assert p.passed and p.token_cap == 10**9
    assert started.count("planner") == planned
    resumed = [e for e in p.log if e["type"] == "resumed"][-1]
    assert resumed["token_cap"] == 10**9


def test_retry_a_halted_build_with_more_fix_attempts(env, monkeypatch):
    import foundry.cli

    calls: list[dict[str, Any]] = []

    def fake_run(request, **kw):
        calls.append(kw)
        passed = len(calls) > 1
        return SimpleNamespace(
            passed=passed,
            phase="emitted" if passed else "halted",
            mode="assistant",
            artifact_uri=None,
            gate_statuses={},
        )

    monkeypatch.setattr(foundry.cli, "run", fake_run)
    monkeypatch.setattr(server, "_has_durable_checkpoint", lambda _tid: True)
    p = _new(env)
    _wait_ended(p)
    assert p.phase == "halted" and calls[0]["max_fix_attempts"] == 3

    assert (
        _call(env, "POST", f"/api/projects/{p.id}/resume", {"max_fix_attempts": 99})[0] == 400
    )  # out of range
    for junk in (
        {"token_cap": "lots"},
        {"token_cap": True},
        {"token_cap": 1.5},
        {"max_fix_attempts": -1},
    ):
        assert _call(env, "POST", f"/api/projects/{p.id}/resume", junk)[0] == 400, junk
    code, _ = _call(env, "POST", f"/api/projects/{p.id}/resume", {"max_fix_attempts": 6})
    assert code == 200
    _wait_ended(p)
    assert calls[1]["max_fix_attempts"] == 6 and calls[1]["thread_id"] == p.id
    assert p.passed and p.phase == "passed"
    assert _call(env, "POST", f"/api/projects/{p.id}/resume")[0] == 409  # shipped


def test_resume_a_time_capped_build_with_a_new_time_cap(env, monkeypatch):
    import foundry.cli
    from foundry.proxy.token_meter import TimeCapExceeded
    from webapp import insight

    calls: list[dict[str, Any]] = []

    def fake_run(request, **kw):
        calls.append(kw)
        if len(calls) == 1:
            ledger = insight.ledger_path(server._STATE_DIR, kw["thread_id"])
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps({"used": 10, "elapsed_s": 30 * 60}))
            raise TimeCapExceeded("time cap reached: 30.0 of 30.0 minutes used")
        return SimpleNamespace(
            passed=True, phase="emitted", mode="assistant", artifact_uri=None, gate_statuses={}
        )

    monkeypatch.setattr(foundry.cli, "run", fake_run)
    monkeypatch.setattr(server, "_has_durable_checkpoint", lambda _tid: True)
    p = _new(env, time_cap_minutes=30)
    _wait_ended(p)
    assert p.phase == "time_cap" and calls[0]["time_cap_minutes"] == 30

    resume = f"/api/projects/{p.id}/resume"
    for junk in (
        {"time_cap_minutes": "soon"},
        {"time_cap_minutes": True},
        {"time_cap_minutes": -5},
        {"time_cap_minutes": 10**7},
        {"time_cap_minutes": float("inf")},
    ):
        assert _call(env, "POST", resume, junk)[0] == 400, junk
    code, got = _call(env, "POST", resume, {"time_cap_minutes": 25})
    assert code == 400 and "30.0 minutes already spent" in got["error"]
    assert p.build["time_cap_minutes"] == 30 and p.phase == "time_cap"  # untouched

    assert _call(env, "POST", resume, {"time_cap_minutes": 90})[0] == 200
    _wait_ended(p)
    assert calls[1]["time_cap_minutes"] == 90 and p.passed
    resumed = [e for e in p.log if e["type"] == "resumed"][-1]
    assert resumed["time_cap_minutes"] == 90
    assert (
        "90-minute time cap"
        in [e for e in p.log if e["type"] == "narrate" and e["text"].startswith("▶")][-1]["text"]
    )

    # A restart re-reads the cap the latest resume set, so the next Resume keeps it.
    server._PROJECTS.clear()
    server._load_persisted()
    assert server._PROJECTS[p.id].build["time_cap_minutes"] == 90


def test_resume_with_time_cap_zero_lifts_the_cap(env, monkeypatch):
    p = Project(
        id="t0",
        request="r",
        mode="assistant",
        floor=1,
        status="done",
        ended=True,
        phase="stopped",
        build={"time_cap_minutes": 5},
    )
    server._PROJECTS["t0"] = p
    monkeypatch.setattr(server, "_gated_runner", lambda *a, **k: None)
    assert _call(env, "POST", "/api/projects/t0/resume", {"time_cap_minutes": 0})[0] == 200
    assert p.build["time_cap_minutes"] is None


def test_resume_and_stop_refuse_the_wrong_state(env):
    running = Project(id="r1", request="r", mode="assistant", floor=1)
    server._PROJECTS["r1"] = running
    assert _call(env, "POST", "/api/projects/r1/resume")[0] == 409
    assert _call(env, "POST", "/api/projects/nope/stop")[0] == 404
    gone = Project(
        id="i1",
        request="r",
        mode="assistant",
        floor=2,
        status="done",
        ended=True,
        phase="interrupted",
    )
    server._PROJECTS["i1"] = gone
    assert _call(env, "POST", "/api/projects/i1/stop")[0] == 409
    code, got = _call(env, "POST", "/api/projects/i1/resume")
    assert code == 409 and "checkpoint" in got["error"]  # nothing durable to resume


def test_restart_restores_the_controls_from_the_log(env):
    for i, ev in enumerate(
        [
            {
                "type": "project",
                "token_cap": 5,
                "max_fix_attempts": 3,
                "workers": {"backend": 1},
                "ticket_tiers": {"trivial": "T3"},
            },
            {"type": "profile", "profile": "python"},
            {"type": "done", "passed": False, "phase": "token_cap"},
            {"type": "_end"},
            {"type": "resumed", "token_cap": 50, "max_fix_attempts": 6},
            {"type": "done", "passed": False, "phase": "paused"},
            {"type": "_end"},
        ]
    ):
        env.store.append_event("c1", i, ev)
    env.store.upsert(
        {
            "id": "c1",
            "floor": 1,
            "request": "r",
            "mode": "assistant",
            "status": "done",
            "passed": False,
        }
    )
    server._load_persisted()
    p = server._PROJECTS["c1"]
    assert (p.token_cap, p.max_fix_attempts, p.phase) == (50, 6, "paused")
    # …and the settings it ran with, so a Resume runs it the same way
    assert (p.workers["backend"], p.ticket_tiers, p.profile) == (1, {"trivial": "T3"}, "python")


# --- delete ------------------------------------------------------------------- #


def test_delete_refuses_while_running_and_removes_one_floor_otherwise(env):
    out = env.tmp / "out"
    keep = Project(
        id="k1",
        request="keep",
        mode="assistant",
        floor=1,
        status="done",
        ended=True,
        out_base=out / "floor-1",
    )
    drop = Project(id="d1", request="drop", mode="assistant", floor=2, out_base=out / "floor-2")
    for q in (keep, drop):
        q.out_base.mkdir(parents=True)
        (q.out_base / "f.txt").write_text("x")
        server._PROJECTS[q.id] = q
        server._emit(q, type="project", id=q.id)

    assert _call(env, "DELETE", "/api/projects/d1")[0] == 409  # still running
    assert "d1" in server._PROJECTS and (out / "floor-2").is_dir()

    drop.status = "done"
    server._close(drop)
    assert _call(env, "DELETE", "/api/projects/d1")[0] == 200
    assert "d1" not in server._PROJECTS and not (out / "floor-2").exists()
    assert env.store.events_of("d1") == [] and env.store.get("d1") is None
    # the other floor is untouched
    assert "k1" in server._PROJECTS and (out / "floor-1" / "f.txt").is_file()
    assert env.store.get("k1") is not None and env.store.events_of("k1")
    assert _call(env, "DELETE", "/api/projects/d1")[0] == 404


# --- the guard on the new routes ---------------------------------------------- #


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/projects/x1/stop"),
        ("POST", "/api/projects/x1/pause"),
        ("POST", "/api/projects/x1/resume"),
        ("DELETE", "/api/projects/x1"),
    ],
)
def test_new_routes_enforce_csrf_token_and_sign_in(env, monkeypatch, method, path):
    server._PROJECTS["x1"] = Project(id="x1", request="r", mode="assistant", floor=1)
    evil = {"Origin": "http://evil.example", "Host": f"127.0.0.1:{env.port}"}
    code, got = _call(env, method, path, headers=evil)
    assert code == 403 and "CSRF" in got["error"]

    monkeypatch.setattr(server, "_AUTH_TOKEN", "s3cret")
    assert _call(env, method, path)[0] == 403  # token missing
    monkeypatch.setattr(server, "_AUTH_TOKEN", None)

    monkeypatch.setenv("FOUNDRY_AUTH", "1")
    assert _call(env, method, path)[0] == 401  # not signed in
    assert server._PROJECTS["x1"].status == "running"  # nothing happened


# --- the build list: search, filter, sort -------------------------------------- #


def _floor(pid: str, floor: int, request: str, created: str, **kw: Any) -> Project:
    p = Project(id=pid, request=request, mode="assistant", floor=floor, created_at=created, **kw)
    server._PROJECTS[pid] = p
    return p


@pytest.fixture
def floors(env):
    done = {"status": "done", "ended": True}
    _floor(
        "s1",
        1,
        "Build me a URL shortener with custom slugs.",
        "2026-01-01T00:00:00",
        passed=True,
        phase="passed",
        **done,
    )
    _floor("h1", 2, "a todo app\nwith tags", "2026-01-03T00:00:00", phase="halted", **done)
    _floor("p1", 3, "Pomodoro timer", "2026-01-02T00:00:00", phase="paused", **done)
    _floor("t1", 4, "Shortener analytics", "2026-01-04T00:00:00", phase="token_cap", **done)
    _floor("r1", 5, "Weather dashboard", "2026-01-05T00:00:00")
    return env


def _ids(env: Any, query: str = "") -> list[str]:
    code, got = _call(env, "GET", "/api/builds" + query)
    assert code == 200, got
    return [b["id"] for b in got["builds"]]


def test_builds_list_every_floor_newest_first_with_a_status_and_title(floors):
    code, got = _call(floors, "GET", "/api/builds")
    assert code == 200 and got["total"] == 5
    assert [b["id"] for b in got["builds"]] == ["r1", "t1", "h1", "p1", "s1"]
    by = {b["id"]: b for b in got["builds"]}
    assert {k: by[k]["status"] for k in by} == {
        "s1": "shipped",
        "h1": "halted",
        "p1": "stopped",
        "t1": "halted",
        "r1": "running",
    }
    assert by["s1"]["title"] == "URL shortener with custom slugs"
    assert by["h1"]["title"] == "Todo app"
    assert _ids(floors, "?sort=oldest") == ["s1", "p1", "h1", "t1", "r1"]


def test_builds_filter_by_text_and_status(floors):
    assert _ids(floors, "?q=shortener") == ["t1", "s1"]
    assert _ids(floors, "?q=SHORTENER%20custom") == ["s1"]  # every word, any case
    assert _ids(floors, "?q=tags") == ["h1"]  # the whole request
    assert _ids(floors, "?status=halted") == ["t1", "h1"]
    assert _ids(floors, "?status=stopped") == ["p1"]
    assert _ids(floors, "?status=running") == ["r1"]
    assert _ids(floors, "?status=shipped&q=shortener") == ["s1"]
    assert _ids(floors, "?status=all&q=&sort=") == ["r1", "t1", "h1", "p1", "s1"]
    assert _ids(floors, "?q=nothing-like-this") == []


@pytest.mark.parametrize(
    "query,why",
    [
        ("?status=done", "status must be"),
        ("?sort=popular", "sort must be"),
        ("?q=" + "x" * 201, "longer than"),
        ("?page=2", "unknown query parameter"),
        ("?status=halted&status=shipped", "more than once"),
    ],
)
def test_builds_refuse_bad_query_params(floors, query, why):
    code, got = _call(floors, "GET", "/api/builds" + query)
    assert code == 400 and why in got["error"]


def test_request_title_names_a_floor_from_its_request():
    t = server.request_title
    assert t("please create an inventory tracker. It should…") == "Inventory tracker"
    assert t("  ") == "Untitled build"
    long = t("a " + "very " * 30 + "long request")
    assert long.endswith("…") and len(long) <= 49 and "  " not in long
