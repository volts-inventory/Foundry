"""Workshop build settings: the saved model mode / level table / difficulty
mapping (defaults for new builds), the New Project form's advanced fields, the
local-server check, and "Change this build" (plan Phases 4, 4b, 5.1)."""

from __future__ import annotations

import http.client
import inspect
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import foundry.cli
import foundry.proxy.local_transport as local_transport
from foundry.proxy.local_transport import LocalServerStatus
from webapp import server
from webapp import settings as ws
from webapp.server import Project
from webapp.store import BuildStore


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
    monkeypatch.setenv("FOUNDRY_AUTH", "0")
    monkeypatch.setenv("FOUNDRY_MODEL_BACKEND", "mock")
    monkeypatch.delenv("FOUNDRY_TOKEN_CAP", raising=False)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=srv.server_port, store=store, tmp=tmp_path)
    srv.shutdown()


@pytest.fixture
def runs(monkeypatch) -> list[dict[str, Any]]:
    """Replace ``run`` with a spy that checks the call against the real signature
    (a misspelt keyword fails here) and ships at once."""
    calls: list[dict[str, Any]] = []
    sig = inspect.signature(foundry.cli.run)

    def spy(*args: Any, **kwargs: Any) -> Any:
        sig.bind(*args, **kwargs)
        spec = kwargs.get("spec")
        base = kwargs.get("base_dir")
        calls.append({**kwargs,
                      "_spec_text": Path(spec).read_text(encoding="utf-8") if spec else None,
                      "_base_files": sorted(str(p.relative_to(base)) for p in Path(base).rglob("*")
                                            if p.is_file()) if base else None})
        return SimpleNamespace(passed=True, phase="passed", mode="assistant",
                               artifact_uri=None, gate_statuses={})

    monkeypatch.setattr(foundry.cli, "run", spy)
    return calls


def _call(env: Any, method: str, path: str, body: Any = None,
          headers: dict[str, str] | None = None, raw: bytes | None = None
          ) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=30)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=data,
                 headers={"Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    out = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, out


def _wait_ended(p: Project, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not (p.ended and p.status == "done") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert p.ended and p.status == "done", "build did not finish in time"


def _build(env: Any, runs: list[dict[str, Any]], **body: Any) -> tuple[Project, dict[str, Any]]:
    code, got = _call(env, "POST", "/api/projects", {"request": "build a thing", **body})
    assert code == 200, got
    p = server._PROJECTS[got["id"]]
    _wait_ended(p)
    return p, runs[-1]


# --- settings ------------------------------------------------------------------ #

def test_default_settings_are_cloud_with_the_preset_table(env):
    code, got = _call(env, "GET", "/api/settings")
    assert code == 200
    s = got["settings"]
    assert s["model_mode"] == "cloud"
    assert s["ticket_tiers"] == {"trivial": "T3", "standard": "T2", "complex": "T1"}
    assert s["levels"] == got["presets"]["cloud"]
    assert got["presets"]["hybrid"]["T2"]["backend"] == "local"
    assert got["presets"]["local"]["T1"]["model"] == s["local_model"]
    assert "python" in got["profiles"] and got["scopes"] == ["auto", "demo", "standard"]
    assert got["fix_defaults"] == {"cloud": 3, "hybrid": 4, "local": 5}


def test_saved_settings_persist_and_become_the_defaults_for_new_builds(env, runs):
    preset = _call(env, "GET", "/api/settings")[1]["presets"]["hybrid"]
    levels = {**preset, "T1": {"backend": "anthropic", "model": "claude-custom-1"}}
    body = {"model_mode": "hybrid", "levels": levels,
            "ticket_tiers": {"trivial": "T2", "standard": "T2", "complex": "T1"},
            "local_url": "http://127.0.0.1:9999/v1", "local_model": "qwen/qwen3.6-27b"}
    code, got = _call(env, "POST", "/api/settings", body)
    assert code == 200, got
    assert (env.tmp / "out" / "settings.json").is_file()
    s = _call(env, "GET", "/api/settings")[1]["settings"]
    assert s["model_mode"] == "hybrid" and s["levels"]["T1"]["model"] == "claude-custom-1"
    assert s["levels"]["T2"]["base_url"] == "http://127.0.0.1:9999/v1"

    p, kw = _build(env, runs)
    assert kw["model_mode"] == "hybrid"
    assert kw["levels"]["T1"] == {"backend": "anthropic", "model": "claude-custom-1"}
    assert kw["local_url"] == "http://127.0.0.1:9999/v1"
    assert kw["local_model"] == "qwen/qwen3.6-27b"
    assert kw["max_fix_attempts"] == 4                       # the hybrid default
    assert kw["ticket_tiers"] == {"trivial": "T2", "standard": "T2", "complex": "T1"}
    assert p.build["model_mode"] == "hybrid" and p.build["levels_custom"] is True


def test_an_unedited_preset_is_sent_as_the_mode_alone(env, runs):
    assert _call(env, "POST", "/api/settings", {"model_mode": "local"})[0] == 200
    _, kw = _build(env, runs)
    assert kw["model_mode"] == "local" and kw["levels"] is None
    assert kw["max_fix_attempts"] == 5


@pytest.mark.parametrize("body", [
    {"model_mode": "turbo"},
    {"local_url": "file:///etc/passwd"},
    {"local_url": "http://"},
    {"local_model": "bad model id"},
    {"levels": {"T1": {"backend": "mock", "model": "m"}}},                 # tiers missing
    {"levels": {t: {"backend": "nope", "model": "m"} for t in ("T1", "T2", "T3")}},
    {"levels": {t: {"backend": "mock", "model": "a b"} for t in ("T1", "T2", "T3")}},
    {"ticket_tiers": {"trivial": "T4", "standard": "T2", "complex": "T1"}},
    {"ticket_tiers": {"trivial": "T3"}},
    [1, 2],
])
def test_bad_settings_are_refused_and_nothing_is_saved(env, body):
    code, got = _call(env, "POST", "/api/settings", body)
    assert code == 400, got
    assert not (env.tmp / "out" / "settings.json").exists()


def test_an_unreadable_settings_file_falls_back_to_defaults(env):
    (env.tmp / "out").mkdir(parents=True, exist_ok=True)
    (env.tmp / "out" / "settings.json").write_text('{"model_mode": "turbo"}')
    assert _call(env, "GET", "/api/settings")[1]["settings"]["model_mode"] == "cloud"


# --- advanced form fields ---------------------------------------------------------- #

def test_every_advanced_field_reaches_run(env, runs):
    p, kw = _build(env, runs, max_fix_attempts=7, time_cap_minutes=45, profile="go",
                   scope="demo", cost_cap_usd=2.5, spec="# Spec\nbe excellent",
                   spec_name="../../brief.md")
    assert kw["max_fix_attempts"] == 7
    assert kw["time_cap_minutes"] == 45
    assert kw["profile"] == "go"
    assert kw["scope"] == "demo"
    assert kw["cost_cap_microusd"] == 2_500_000
    assert kw["_spec_text"] == "# Spec\nbe excellent"
    # the spec is a server-owned file under the floor, never the browser's text as-is
    assert Path(kw["spec"]) == env.tmp / "out" / f"floor-{p.floor}" / ".inputs" / "spec.md"
    assert p.build["spec_name"] == "brief.md"
    assert kw["model_mode"] == "cloud" and kw["levels"] is None
    assert kw.get("base_dir") is None


def test_blank_advanced_fields_mean_the_defaults(env, runs):
    _, kw = _build(env, runs, max_fix_attempts="", time_cap_minutes="", cost_cap_usd=None,
                   spec="   ")
    assert kw["max_fix_attempts"] == 3
    assert kw["time_cap_minutes"] is None and kw["cost_cap_microusd"] is None
    assert kw["profile"] == "auto" and kw["scope"] == "auto"
    assert kw.get("spec") is None


@pytest.mark.parametrize("field,value", [
    ("max_fix_attempts", -1), ("max_fix_attempts", 21), ("max_fix_attempts", 2.5),
    ("max_fix_attempts", True), ("max_fix_attempts", "x"),
    ("time_cap_minutes", 0), ("time_cap_minutes", -5), ("time_cap_minutes", 10**6),
    ("time_cap_minutes", "soon"), ("time_cap_minutes", "nan"),
    ("cost_cap_usd", 0), ("cost_cap_usd", -1), ("cost_cap_usd", 10**9), ("cost_cap_usd", [1]),
    ("profile", "cobol"), ("profile", 3), ("scope", "huge"),
    ("spec", 42), ("spec", ["/etc/passwd"]),
    ("model_mode", "turbo"), ("local_url", "ftp://x"),
])
def test_bad_advanced_values_get_400_and_start_nothing(env, runs, field, value):
    code, got = _call(env, "POST", "/api/projects", {"request": "x", field: value})
    assert code == 400, got
    assert server._PROJECTS == {} and runs == []


def test_spec_size_is_limited(env, runs):
    big = "a" * (ws.SPEC_MAX_BYTES + 1)
    code, got = _call(env, "POST", "/api/projects", {"request": "x", "spec": big})
    assert code == 400 and "KB" in got["error"]
    # multibyte text is measured in bytes, not characters
    wide = "é" * (ws.SPEC_MAX_BYTES // 2 + 1)
    assert _call(env, "POST", "/api/projects", {"request": "x", "spec": wide})[0] == 400
    _, kw = _build(env, runs, spec="a" * ws.SPEC_MAX_BYTES)
    assert len(kw["_spec_text"]) == ws.SPEC_MAX_BYTES


def test_an_oversized_body_is_refused_unread(env, runs):
    raw = json.dumps({"request": "x", "pad": "a" * ws.MAX_BODY_BYTES}).encode()
    for path in ("/api/projects", "/api/settings"):
        code, _ = _call(env, "POST", path, raw=raw)
        assert code == 413
    assert runs == [] and not (env.tmp / "out" / "settings.json").exists()


def test_a_restart_restores_a_floors_build_options(env):
    p = Project(id="r1", request="r", mode="assistant", floor=1,
                out_base=env.tmp / "out" / "floor-1")
    build = {"model_mode": "hybrid", "levels": {"T1": {"backend": "mock", "model": "m"}},
             "levels_custom": True, "local_url": "http://h:1/v1", "local_model": "m",
             "time_cap_minutes": 5, "scope": "demo", "cost_cap_microusd": 9, "spec": True,
             "base_id": "b0", "base_floor": 3}
    p.log = [{"type": "project", "build": build, "profile": "go"}]
    server._restore_controls(p)
    assert p.profile == "go"
    kw = server._build_kwargs(p)
    assert kw["model_mode"] == "hybrid" and kw["levels"] == build["levels"]
    assert kw["time_cap_minutes"] == 5 and kw["scope"] == "demo"
    assert kw["spec"].endswith("floor-1/.inputs/spec.md")
    assert kw["base_dir"].endswith("floor-1/.inputs/base")


# --- local server check ------------------------------------------------------------- #

def test_local_check_reports_reachability_and_served_models(env, monkeypatch):
    seen: list[tuple[str, Any]] = []

    def fake(url, *, model=None, **_kw):
        seen.append((url, model))
        return LocalServerStatus(url=url, reachable=True, models=("a/qwen", "b"), model=model,
                                 model_served=model in ("a/qwen", "b"))

    monkeypatch.setattr(local_transport, "check_local_server", fake)
    code, got = _call(env, "POST", "/api/settings/local-check",
                      {"url": "http://127.0.0.1:1234/v1/", "model": "a/qwen"})
    assert code == 200
    assert got["reachable"] is True and got["models"] == ["a/qwen", "b"]
    assert got["model_served"] is True
    assert seen == [("http://127.0.0.1:1234/v1", "a/qwen")]


@pytest.mark.parametrize("body", [
    {"url": "file:///etc/passwd"}, {"url": "gopher://x"}, {"url": ""}, {"url": 5},
    {"url": "http://x/" + "a" * 600}, {"url": "http://x", "model": "bad id"}, ["x"],
])
def test_local_check_validates_the_url_before_any_request(env, monkeypatch, body):
    called: list[Any] = []
    monkeypatch.setattr(local_transport, "check_local_server",
                        lambda *a, **k: called.append(a))
    code, _ = _call(env, "POST", "/api/settings/local-check", body)
    assert code == 400 and called == []


def test_local_check_handles_an_unreachable_server(env, monkeypatch):
    monkeypatch.setattr(
        local_transport, "check_local_server",
        lambda url, *, model=None, **_: LocalServerStatus(
            url=url, reachable=False, error="connection refused", model=model))
    code, got = _call(env, "POST", "/api/settings/local-check", {"url": "http://127.0.0.1:9/v1"})
    assert code == 200
    assert got["reachable"] is False and got["error"] == "connection refused"
    assert got["models"] == []


# --- change a build -------------------------------------------------------------- #

def _shipped(env: Any, pid: str, floor: int, passed: bool = True) -> Project:
    out = env.tmp / "out" / f"floor-{floor}"
    (out / "code" / "src").mkdir(parents=True)
    (out / "code" / "src" / "app.py").write_text("print('hi')\n")
    (out / "code" / "README.md").write_text("# app\n")
    p = Project(id=pid, request="the app", mode="assistant", floor=floor, status="done",
                ended=True, passed=passed, out_base=out)
    server._PROJECTS[pid] = p
    server._FLOOR[0] = max(server._FLOOR[0], floor)
    return p


def test_change_a_build_resolves_the_base_tree_server_side(env, runs):
    _shipped(env, "base1", 1)
    p, kw = _build(env, runs, request="add a dark mode", base_id="base1",
                   base_dir="/etc", out_base="/tmp/evil")
    assert p.floor == 2
    assert Path(kw["base_dir"]) == env.tmp / "out" / "floor-2" / ".inputs" / "base"
    assert kw["_base_files"] == ["README.md", "src/app.py"]
    assert kw["thread_id"] == p.id != "base1"
    assert p.build["base_id"] == "base1" and p.build["base_floor"] == 1
    assert p.out_base == env.tmp / "out" / "floor-2"
    project_ev = next(e for e in p.log if e["type"] == "project")
    assert project_ev["build"]["base_floor"] == 1
    assert any("Change of floor 1" in str(e.get("text")) for e in p.log)
    rows = _call(env, "GET", "/api/projects")[1]["projects"]
    assert next(r for r in rows if r["id"] == p.id)["build"]["base_floor"] == 1


def test_change_a_shipped_mock_floor_ships_through_the_workshop(env):
    def build(**body: Any) -> Project:
        code, got = _call(env, "POST", "/api/projects", body)
        assert code == 200, got
        p = server._PROJECTS[got["id"]]
        _wait_ended(p, timeout=120)
        return p

    base = build(request="build a todo app")
    assert base.passed, [e for e in base.log if e["type"] == "halt"]
    change = build(request="add a dark mode", base_id=base.id)
    staged = env.tmp / "out" / f"floor-{change.floor}" / ".inputs" / "base"
    # The base is the tree the base floor's gates judged, with its manifest — not
    # scenarios the mock gates never ran.
    assert not (staged / "features").exists()
    manifest = json.loads((staged / "BUILD-MANIFEST.json").read_text())
    assert manifest["thread_id"] == base.id and manifest["stack"]["language"] == "python"
    assert change.passed, [e for e in change.log if e["type"] == "halt"]


def test_change_prefers_the_floors_own_export_with_its_manifest(env, runs):
    base = _shipped(env, "base1", 1)
    assert base.out_base is not None
    (base.out_base / "code" / "BUILD-MANIFEST.json").write_text('{"thread_id": "base1"}')
    _, kw = _build(env, runs, request="add a dark mode", base_id="base1")
    assert kw["_base_files"] == ["BUILD-MANIFEST.json", "README.md", "src/app.py"]


def test_change_refuses_unknown_and_unshipped_floors(env, runs):
    _shipped(env, "held", 1, passed=False)
    running = Project(id="live", request="r", mode="assistant", floor=2, passed=True)
    server._PROJECTS["live"] = running
    assert _call(env, "POST", "/api/projects", {"request": "x", "base_id": "nope"})[0] == 404
    assert _call(env, "POST", "/api/projects", {"request": "x", "base_id": "held"})[0] == 409
    assert _call(env, "POST", "/api/projects", {"request": "x", "base_id": "live"})[0] == 409
    assert runs == [] and set(server._PROJECTS) == {"held", "live"}


def test_change_refuses_a_shipped_floor_with_no_readable_tree(env, runs):
    server._PROJECTS["gone"] = Project(id="gone", request="r", mode="assistant", floor=1,
                                      status="done", ended=True, passed=True,
                                      out_base=env.tmp / "out" / "floor-1")
    code, _ = _call(env, "POST", "/api/projects", {"request": "x", "base_id": "gone"})
    assert code == 409 and runs == []


def test_write_tree_refuses_a_path_that_escapes(tmp_path):
    with pytest.raises(ValueError):
        ws.write_tree(tmp_path / "base", {"../evil.txt": b"x"})
    assert not (tmp_path / "evil.txt").exists()


# --- the guard on the new routes ----------------------------------------------------- #

@pytest.mark.parametrize("path", ["/api/settings", "/api/settings/local-check"])
def test_new_routes_enforce_csrf_token_and_sign_in(env, monkeypatch, path):
    # No body: the guard answers before reading one, and an unread body would be
    # parsed as the connection's next request.
    body = None
    called: list[Any] = []
    monkeypatch.setattr(local_transport, "check_local_server",
                        lambda *a, **k: called.append(a))
    evil = {"Origin": "http://evil.example", "Host": f"127.0.0.1:{env.port}"}
    code, got = _call(env, "POST", path, body, headers=evil)
    assert code == 403 and "CSRF" in got["error"]

    monkeypatch.setattr(server, "_AUTH_TOKEN", "s3cret")
    assert _call(env, "POST", path, body)[0] == 403
    monkeypatch.setattr(server, "_AUTH_TOKEN", None)

    monkeypatch.setenv("FOUNDRY_AUTH", "1")
    assert _call(env, "POST", path, body)[0] == 401
    assert _call(env, "GET", "/api/settings")[0] == 401
    assert called == [] and not (env.tmp / "out" / "settings.json").exists()
    assert server._PROJECTS == {}
