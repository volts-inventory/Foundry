"""Workshop insight: the halt panel, the live meter, the build queue, request-body
limits, the draft tree on a halt and the run card on a ship (webapp/insight.py)."""

from __future__ import annotations

import http.client
import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from webapp import insight, server
from webapp.events import events_for_result
from webapp.server import Project
from webapp.store import BuildStore

# Build PASS, Test ERROR (a test imports a missing module) -> the floor halts at the gate.
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = BuildStore(tmp_path / "foundry.db")
    monkeypatch.setattr(server, "_STORE", store)
    monkeypatch.setattr(server, "_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "_OUTPUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(server, "_PROJECTS", {})
    monkeypatch.setattr(server, "_FLOOR", [0])
    monkeypatch.setattr(server, "_PACE", 0)
    monkeypatch.setattr(server, "_BUILD_GATE", None)
    monkeypatch.setattr(server, "_AUTH_TOKEN", None)
    monkeypatch.setattr(server, "_REPO_CACHE", {})
    monkeypatch.setattr(insight, "QUEUE", insight.BuildQueue())
    monkeypatch.setenv("FOUNDRY_AUTH", "0")
    monkeypatch.setenv("FOUNDRY_MODEL_BACKEND", "mock")
    monkeypatch.delenv("FOUNDRY_TOKEN_CAP", raising=False)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=srv.server_port, store=store, tmp=tmp_path)
    srv.shutdown()


def _raw(
    env: Any,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=60)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, data


def _call(env: Any, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    code, _, raw = _raw(env, method, path, data, {"Content-Type": "application/json"})
    return code, json.loads(raw or b"{}")


def _wait_ended(p: Project, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while not (p.ended and p.status == "done") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert p.ended and p.status == "done", "build did not finish in time"


def _new(env: Any, **body: Any) -> Project:
    code, got = _call(env, "POST", "/api/projects", {"request": "build a todo api", **body})
    assert code == 200, got
    return server._PROJECTS[got["id"]]


def _failing_gates(monkeypatch) -> None:
    import foundry.cli

    real = foundry.cli.run

    def run(request: str, **kw: Any) -> Any:
        return real(request, **{**kw, "diff_body": _ERROR_DIFF, "max_fix_attempts": 0})

    monkeypatch.setattr(foundry.cli, "run", run)


# --- request bodies ----------------------------------------------------------- #


def _body(raw: bytes, length: str | None = None) -> dict[str, Any]:
    headers = {"Content-Length": str(len(raw)) if length is None else length}
    return insight.read_json_body(headers, io.BytesIO(raw), limit=64)


def test_read_json_body_caps_size_and_names_bad_json() -> None:
    assert _body(b"") == {} and _body(b"null") == {}
    assert _body(b'{"a": 1}') == {"a": 1}
    with pytest.raises(insight.BodyError) as big:
        _body(b"{}", length="65")
    assert big.value.status == 413 and "too large" in big.value.message
    with pytest.raises(insight.BodyError) as bad:
        _body(b'{"a": ')
    assert bad.value.status == 400 and "invalid JSON" in bad.value.message
    assert "line 1" in bad.value.message
    for junk, why in ((b"[1, 2]", "JSON object"), (b"\xff\xfe", "UTF-8")):
        with pytest.raises(insight.BodyError) as e:
            _body(junk)
        assert e.value.status == 400 and why in e.value.message
    with pytest.raises(insight.BodyError):
        _body(b"{}", length="12abc")


def test_post_routes_answer_bad_bodies_with_a_real_error(env, monkeypatch):
    code, _, raw = _raw(
        env, "POST", "/api/projects", b'{"request": "x",', {"Content-Type": "application/json"}
    )
    assert code == 400 and "invalid JSON" in json.loads(raw)["error"]
    monkeypatch.setattr(insight, "MAX_BODY_BYTES", 100)
    code, _, raw = _raw(
        env, "POST", "/api/projects", b"{" + b" " * 200 + b"}", {"Content-Type": "application/json"}
    )
    assert code == 413 and "too large" in json.loads(raw)["error"]
    assert server._PROJECTS == {}


# --- the live meter ------------------------------------------------------------ #


def test_meter_watch_reads_the_ledger_and_emits_on_change(tmp_path) -> None:
    ledger = insight.ledger_path(tmp_path, "run-1")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"used": 100, "elapsed_s": 30.0}))
    got: list[dict[str, Any]] = []
    watch = insight.MeterWatch(
        tmp_path, "run-1", got.append, caps=lambda: (1000, None), interval=0.02, min_gap=0
    )
    watch.start()
    assert got[0]["type"] == "meter" and got[0]["used"] == 100 and got[0]["cap"] == 1000
    assert got[0]["elapsed_s"] >= 30.0
    ledger.write_text(json.dumps({"used": 250, "elapsed_s": 31.0}))
    deadline = time.monotonic() + 5
    while len(got) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    final = watch.stop()
    assert got[1]["used"] == 250 and final["used"] == 250
    n = len(got)
    time.sleep(0.1)
    assert len(got) == n  # stopped: nothing after the final reading


def test_a_build_streams_meter_events_and_done_carries_the_total(env):
    p = _new(env)
    _wait_ended(p)
    meters = [e for e in p.log if e["type"] == "meter"]
    assert meters and meters[0]["cap"] is None
    done = [e for e in p.log if e["type"] == "done"][-1]
    assert done["meter"]["used"] > 0 and done["meter"]["elapsed_s"] >= 0
    # no reading may land after the floor closed
    end = max(i for i, e in enumerate(p.log) if e["type"] == "_end")
    assert end == len(p.log) - 1
    code, got = _call(env, "GET", "/api/projects")
    assert code == 200 and got["projects"][0]["meter"]["used"] == done["meter"]["used"]


# --- the build queue ------------------------------------------------------------ #


def test_queued_floors_get_fifo_positions_and_start_in_order(env, monkeypatch):
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(server, "_BUILD_GATE", gate)
    monkeypatch.setattr(server, "_MAX_CONCURRENT_BUILDS", 1)
    gate.acquire()  # every slot busy
    a = _new(env)
    b = _new(env)
    deadline = time.monotonic() + 10
    while insight.QUEUE.position(b.id) != 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    rows = {r["id"]: r for r in _call(env, "GET", "/api/projects")[1]["projects"]}
    assert (rows[a.id]["queue"], rows[b.id]["queue"]) == (1, 2)
    assert rows[a.id]["status"] == "queued"
    gate.release()
    _wait_ended(a)
    _wait_ended(b)
    qa = [e["position"] for e in a.log if e["type"] == "queue"]
    qb = [e["position"] for e in b.log if e["type"] == "queue"]
    assert qa == [1, 0] and qb[0] == 2 and 1 in qb and qb[-1] == 0
    first_meter = {
        q.id: next(i for i, e in enumerate(q.log) if e["type"] == "meter") for q in (a, b)
    }
    assert first_meter[a.id] > 0 and a.passed and b.passed
    assert insight.QUEUE.head() is None


def test_a_floor_stopped_while_queued_leaves_the_line(env, monkeypatch):
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(server, "_BUILD_GATE", gate)
    gate.acquire()
    a = _new(env)
    b = _new(env)
    deadline = time.monotonic() + 10
    while insight.QUEUE.position(b.id) != 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _call(env, "POST", f"/api/projects/{a.id}/stop")[0] == 200
    _wait_ended(a)
    deadline = time.monotonic() + 10
    while insight.QUEUE.position(b.id) != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert insight.QUEUE.position(b.id) == 1 and a.phase == "stopped"
    assert [e["position"] for e in b.log if e["type"] == "queue"][-1] == 1
    gate.release()
    _wait_ended(b)
    assert b.passed


# --- the halt panel ------------------------------------------------------------- #


def _report(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phase": "halted",
        "reason": "",
        "token_cap": None,
        "max_fix_attempts": 3,
        "resumable": True,
        "state": None,
        "gates": {},
        "meter": None,
        "draft_files": 0,
    }
    return insight.halt_report(**{**base, **kw})


def _kinds(r: dict[str, Any]) -> list[str]:
    return [a["kind"] for a in r["actions"]]


def test_halt_report_says_what_to_do_for_each_phase() -> None:
    tok = _report(
        phase="token_cap", token_cap=50_000, meter={"used": 51_234, "cap": 50_000, "elapsed_s": 12}
    )
    assert tok["title"] == "Out of tokens" and "51,234 of its 50,000-token cap" in tok["cause"]
    raise_cap = tok["actions"][0]
    assert raise_cap["kind"] == "resume" and raise_cap["body"]["token_cap"] >= 100_000
    assert "Raise the token cap" in raise_cap["label"] and raise_cap["primary"]
    assert {"token_cap": 0} in [a.get("body") for a in tok["actions"]]

    for phase in ("stopped", "paused"):
        r = _report(phase=phase)
        assert r["actions"][0] == {"kind": "resume", "label": "Resume", "body": {}, "primary": True}
        assert phase in r["cause"]

    err = _report(
        phase="error",
        reason="Traceback (most recent call last):\n"
        "  File x\nValueError: the planner returned nothing",
    )
    assert "Traceback" not in err["cause"] and "File x" in err["cause"]
    assert err["detail"].startswith("Traceback")

    lost = _report(phase="interrupted", resumable=False)
    assert lost["actions"] == [] and "new build" in lost["next"]

    timed = _report(phase="time_cap", meter={"used": 1, "cap": None, "elapsed_s": 3600})
    assert "1h 00m" in timed["cause"] and _kinds(timed) == ["resume", "resume", "retry"]
    assert timed["actions"][0]["body"] == {"time_cap_minutes": 120}  # past the 60 spent
    assert "120 minutes" in timed["actions"][0]["label"]
    assert timed["actions"][1]["body"] == {"time_cap_minutes": 0}
    capped = _report(
        phase="time_cap", time_cap_s=1800, meter={"used": 1, "cap": None, "elapsed_s": 1800}
    )
    assert capped["actions"][0]["body"] == {"time_cap_minutes": 60}

    stuck = _report(max_fix_attempts=20, draft_files=3)
    assert "maximum" in stuck["next"] and _kinds(stuck)[-1] == "files"
    assert stuck["actions"][-1]["label"] == "Open the draft (3 files)"


def test_halt_report_reads_failing_gates_findings_and_fix_notes_from_state() -> None:
    from foundry.schemas.base import Seq

    def seq(n: int) -> Seq:
        return Seq(run_id="r", emission_epoch=1, superstep_index=n, node_id="x", local=0)

    gate = SimpleNamespace
    state = {
        "gates": [
            gate(name="test", status="GateStatus.FAIL", seq=seq(1), findings=("old",)),
            gate(
                name="test",
                status="GateStatus.ERROR",
                seq=seq(5),
                findings=tuple(f"E{i}: " + "x" * 900 for i in range(9)),
            ),
            gate(name="build", status="GateStatus.PASS", seq=seq(4), findings=()),
        ],
        "gate_triage": [
            gate(name="spec", produced_by="planner", explanation="advisory", seq=seq(1)),
            gate(name="test", produced_by="fixer", explanation="fixed the import", seq=seq(6)),
            gate(
                name="ticket-failed", produced_by="tickets", explanation="T-002 failed", seq=seq(3)
            ),
        ],
        "budget": SimpleNamespace(fix_attempts_total=3),
    }
    r = _report(state=state)
    assert r["title"] == "Held at the gate"
    assert "The TEST gate did not pass after 3 fix attempts" in r["cause"]
    [failing] = r["gates"]
    assert failing["status"] == "error" and len(failing["findings"]) == 6
    assert failing["more"] == 3 and len(failing["findings"][0]) <= 600
    assert failing["findings"][0].endswith("…")
    assert r["passed_gates"] == ["build"] and r["fixes"] == {"attempts": 3, "limit": 3}
    assert [n["text"] for n in r["notes"]] == ["T-002 failed", "fixed the import"]
    assert r["actions"][0]["body"] == {"max_fix_attempts": 6}
    assert _kinds(r) == ["resume", "retry"]


def test_a_gate_halt_reports_findings_and_lists_the_draft(env, monkeypatch):
    _failing_gates(monkeypatch)
    p = _new(env)
    _wait_ended(p)
    assert p.phase == "halted" and not p.passed
    halt = [e for e in p.log if e["type"] == "halt"][-1]
    assert "TEST" in halt["reason"]

    code, got = _call(env, "GET", f"/api/projects/{p.id}/insight")
    assert code == 200 and got["card"] is None
    h = got["halt"]
    assert h["title"] == "Held at the gate" and "TEST" in h["cause"]
    test_gate = next(g for g in h["gates"] if g["name"] == "test")
    assert any("this_module_does_not_exist_xyz" in f for f in test_gate["findings"])
    assert h["resumable"] and h["actions"][0]["body"] == {"max_fix_attempts": 6}
    assert h["draft_files"] > 0 and "files" in _kinds(h)

    code, files = _call(env, "GET", f"/api/files/{p.id}")
    assert code == 200 and files["draft"] is True
    assert "app.py" in [f["path"] for f in files["files"]]


def test_a_token_capped_floor_offers_to_raise_the_cap(env):
    p = _new(env, token_cap=1)
    _wait_ended(p)
    assert p.phase == "token_cap"
    h = _call(env, "GET", f"/api/projects/{p.id}/insight")[1]["halt"]
    assert h["title"] == "Out of tokens" and h["meter"]["cap"] == 1
    assert h["actions"][0]["body"]["token_cap"] > h["meter"]["used"]
    code, _ = _call(env, "POST", f"/api/projects/{p.id}/resume", h["actions"][0]["body"])
    assert code == 200
    _wait_ended(p)
    assert p.passed


def test_insight_route_404s_and_a_running_floor_has_no_report(env, monkeypatch):
    assert _call(env, "GET", "/api/projects/nope/insight")[0] == 404
    p = Project(id="live1", request="x", mode="assistant", floor=1)
    server._PROJECTS[p.id] = p
    got = _call(env, "GET", "/api/projects/live1/insight")[1]
    assert got["halt"] is None and got["card"] is None and got["status"] == "running"


# --- the run card --------------------------------------------------------------- #


def test_a_shipped_floor_gets_run_instructions_from_its_manifest(env):
    p = _new(env)
    _wait_ended(p)
    assert p.passed and p.out_base is not None
    manifest = json.loads((p.out_base / "BUILD-MANIFEST.json").read_text())
    assert manifest["thread_id"] == p.id

    code, got = _call(env, "GET", f"/api/projects/{p.id}/insight")
    assert code == 200 and got["halt"] is None
    card = got["card"]
    assert card["source"] == "manifest" and card["stack"].startswith("python")
    steps = {s["label"]: s["cmd"] for s in card["steps"]}
    assert steps["Test"] == "pytest" and "Run" in steps
    assert card["models"] and all(card["models"].values())
    assert card["calls"] > 0 and card["tokens"]["in"] > 0
    assert card["meter"]["used"] > 0

    code, files = _call(env, "GET", f"/api/files/{p.id}")
    assert code == 200 and files["draft"] is False


def test_run_card_falls_back_to_the_state_stack_and_reads_tickets() -> None:
    log = [
        {"type": "ticket", "id": "T-001", "title": "store"},
        {"type": "ticket", "id": "T-002", "title": "api"},
        {"type": "ticket", "id": "T-003", "title": "dropped", "retired": True},
        {"type": "task", "phase": "start", "ticket_id": "T-001"},
        {"type": "task", "phase": "done", "ticket_id": "T-001"},
        {"type": "task", "phase": "failed", "ticket_id": "T-002"},
    ]
    stack = SimpleNamespace(
        model_dump=lambda: {
            "language": "go",
            "test": "go test ./...",
            "run": "",
            "install": "go mod download",
        }
    )
    card = insight.run_card(
        manifest=None, stack=stack, tickets=insight.tickets_from_log(log), meter=None
    )
    assert card["source"] == "state" and card["stack"] == "go"
    assert [s["label"] for s in card["steps"]] == ["Install", "Test"]
    assert [(t["id"], t["status"]) for t in card["tickets"]] == [
        ("T-001", "done"),
        ("T-002", "failed"),
    ]
    assert insight.find_manifest([None], "x") is None


def test_find_manifest_ignores_another_runs_manifest(tmp_path) -> None:
    (tmp_path / "BUILD-MANIFEST.json").write_text(json.dumps({"thread_id": "old"}))
    assert insight.find_manifest([tmp_path], "new") is None
    assert insight.find_manifest([tmp_path], "old") == {"thread_id": "old"}


# --- the file viewer + the halt reason ------------------------------------------- #


def test_a_file_over_the_view_cap_says_it_was_truncated(env, monkeypatch):
    p = _new(env)
    _wait_ended(p)
    monkeypatch.setattr(server, "_MAX_VIEW_BYTES", 4)
    code, hdrs, data = _raw(env, "GET", f"/api/file/{p.id}?path=app.py")
    assert code == 200 and len(data) == 4
    assert int(hdrs["x-foundry-truncated"]) > 4
    monkeypatch.setattr(server, "_MAX_VIEW_BYTES", 10**6)
    code, hdrs, _ = _raw(env, "GET", f"/api/file/{p.id}?path=app.py")
    assert "x-foundry-truncated" not in hdrs


def test_halt_event_names_the_failing_gates() -> None:
    evs = events_for_result(
        {
            "passed": False,
            "phase": "halted",
            "gate_statuses": {
                "build": "GateStatus.PASS",
                "test": "GateStatus.FAIL",
                "smoke": "GateStatus.ERROR",
            },
        }
    )
    halt = next(e for e in evs if e["type"] == "halt")
    assert halt["reason"].startswith("the SMOKE, TEST gates did not PASS")
    lone = next(
        e for e in events_for_result({"passed": False, "gate_statuses": {}}) if e["type"] == "halt"
    )
    assert lone["reason"].startswith("a gate did not PASS")
