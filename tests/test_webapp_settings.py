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
from webapp import insight, server
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
    for var in ("FOUNDRY_LOCAL_URL", "FOUNDRY_LOCAL_MODEL", "FOUNDRY_MAX_SLOTS"):
        monkeypatch.delenv(var, raising=False)  # the settings' defaults follow them
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
        calls.append(
            {
                **kwargs,
                "_spec_text": Path(spec).read_text(encoding="utf-8") if spec else None,
                "_base_files": sorted(
                    str(p.relative_to(base)) for p in Path(base).rglob("*") if p.is_file()
                )
                if base
                else None,
            }
        )
        return SimpleNamespace(
            passed=True, phase="passed", mode="assistant", artifact_uri=None, gate_statuses={}
        )

    monkeypatch.setattr(foundry.cli, "run", spy)
    return calls


def _call(
    env: Any,
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=30)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(
        method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
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
    assert got["fix_defaults"] == {"cloud": 3, "hybrid": 3, "local": 3}


def test_saved_settings_persist_and_become_the_defaults_for_new_builds(env, runs):
    preset = _call(env, "GET", "/api/settings")[1]["presets"]["hybrid"]
    levels = {**preset, "T1": {"backend": "anthropic", "model": "claude-custom-1"}}
    body = {
        "model_mode": "hybrid",
        "levels": levels,
        "ticket_tiers": {"trivial": "T2", "standard": "T2", "complex": "T1"},
        "local_url": "http://127.0.0.1:9999/v1",
        "local_model": "qwen/qwen3.6-27b",
    }
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
    assert kw["max_fix_attempts"] == 3  # the hybrid default
    assert kw["ticket_tiers"] == {"trivial": "T2", "standard": "T2", "complex": "T1"}
    assert p.build["model_mode"] == "hybrid" and p.build["levels_custom"] is True


def test_an_unedited_preset_is_sent_as_the_mode_alone(env, runs):
    assert _call(env, "POST", "/api/settings", {"model_mode": "local"})[0] == 200
    _, kw = _build(env, runs)
    assert kw["model_mode"] == "local" and kw["levels"] is None
    assert kw["max_fix_attempts"] == 3


@pytest.mark.parametrize(
    "body",
    [
        {"model_mode": "turbo"},
        {"local_url": "file:///etc/passwd"},
        {"local_url": "http://"},
        {"local_model": "bad model id"},
        {"levels": {"T1": {"backend": "mock", "model": "m"}}},  # tiers missing
        {"levels": {t: {"backend": "nope", "model": "m"} for t in ("T1", "T2", "T3")}},
        {"levels": {t: {"backend": "mock", "model": "a b"} for t in ("T1", "T2", "T3")}},
        {"ticket_tiers": {"trivial": "T4", "standard": "T2", "complex": "T1"}},
        {"ticket_tiers": {"trivial": "T3"}},
        [1, 2],
    ],
)
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
    p, kw = _build(
        env,
        runs,
        max_fix_attempts=7,
        time_cap_minutes=45,
        profile="go",
        scope="demo",
        cost_cap_usd=2.5,
        spec="# Spec\nbe excellent",
        spec_name="../../brief.md",
    )
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
    _, kw = _build(
        env, runs, max_fix_attempts="", time_cap_minutes="", cost_cap_usd=None, spec="   "
    )
    assert kw["max_fix_attempts"] == 3
    assert kw["time_cap_minutes"] is None and kw["cost_cap_microusd"] is None
    assert kw["profile"] == "auto" and kw["scope"] == "auto"
    assert kw.get("spec") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_fix_attempts", -1),
        ("max_fix_attempts", 21),
        ("max_fix_attempts", 2.5),
        ("max_fix_attempts", True),
        ("max_fix_attempts", "x"),
        ("time_cap_minutes", 0),
        ("time_cap_minutes", -5),
        ("time_cap_minutes", 10**6),
        ("time_cap_minutes", "soon"),
        ("time_cap_minutes", "nan"),
        ("cost_cap_usd", 0),
        ("cost_cap_usd", -1),
        ("cost_cap_usd", 10**9),
        ("cost_cap_usd", [1]),
        ("profile", "cobol"),
        ("profile", 3),
        ("scope", "huge"),
        ("spec", 42),
        ("spec", ["/etc/passwd"]),
        ("model_mode", "turbo"),
        ("local_url", "ftp://x"),
    ],
)
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
    # Only the declared length is oversized: the server refuses on the header without
    # reading, so a real oversized upload would race the client into a broken pipe.
    raw = json.dumps({"request": "x"}).encode()
    oversized = {"Content-Length": str(insight.MAX_BODY_BYTES + 1)}
    for path in ("/api/projects", "/api/settings", "/api/settings/local-check"):
        code, got = _call(env, "POST", path, raw=raw, headers=oversized)
        assert code == 413, (path, got)
    assert runs == [] and not (env.tmp / "out" / "settings.json").exists()


def test_a_restart_restores_a_floors_build_options(env):
    p = Project(
        id="r1", request="r", mode="assistant", floor=1, out_base=env.tmp / "out" / "floor-1"
    )
    build = {
        "model_mode": "hybrid",
        "levels": {"T1": {"backend": "mock", "model": "m"}},
        "levels_custom": True,
        "local_url": "http://h:1/v1",
        "local_model": "m",
        "time_cap_minutes": 5,
        "scope": "demo",
        "cost_cap_microusd": 9,
        "spec": True,
        "base_id": "b0",
        "base_floor": 3,
    }
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
        return LocalServerStatus(
            url=url,
            reachable=True,
            models=("a/qwen", "b"),
            model=model,
            model_served=model in ("a/qwen", "b"),
        )

    monkeypatch.setattr(local_transport, "check_local_server", fake)
    code, got = _call(
        env,
        "POST",
        "/api/settings/local-check",
        {"url": "http://127.0.0.1:1234/v1/", "model": "a/qwen"},
    )
    assert code == 200
    assert got["reachable"] is True and got["models"] == ["a/qwen", "b"]
    assert got["model_served"] is True
    assert seen == [("http://127.0.0.1:1234/v1", "a/qwen")]


@pytest.mark.parametrize(
    "body",
    [
        {"url": "file:///etc/passwd"},
        {"url": "gopher://x"},
        {"url": ""},
        {"url": 5},
        {"url": "http://x/" + "a" * 600},
        {"url": "http://x", "model": "bad id"},
        ["x"],
    ],
)
def test_local_check_validates_the_url_before_any_request(env, monkeypatch, body):
    called: list[Any] = []
    monkeypatch.setattr(local_transport, "check_local_server", lambda *a, **k: called.append(a))
    code, _ = _call(env, "POST", "/api/settings/local-check", body)
    assert code == 400 and called == []


def test_local_check_handles_an_unreachable_server(env, monkeypatch):
    monkeypatch.setattr(
        local_transport,
        "check_local_server",
        lambda url, *, model=None, **_: LocalServerStatus(
            url=url, reachable=False, error="connection refused", model=model
        ),
    )
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
    p = Project(
        id=pid,
        request="the app",
        mode="assistant",
        floor=floor,
        status="done",
        ended=True,
        passed=passed,
        out_base=out,
    )
    server._PROJECTS[pid] = p
    server._FLOOR[0] = max(server._FLOOR[0], floor)
    return p


def test_change_a_build_resolves_the_base_tree_server_side(env, runs):
    _shipped(env, "base1", 1)
    p, kw = _build(
        env, runs, request="add a dark mode", base_id="base1", base_dir="/etc", out_base="/tmp/evil"
    )
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
    server._PROJECTS["gone"] = Project(
        id="gone",
        request="r",
        mode="assistant",
        floor=1,
        status="done",
        ended=True,
        passed=True,
        out_base=env.tmp / "out" / "floor-1",
    )
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
    monkeypatch.setattr(local_transport, "check_local_server", lambda *a, **k: called.append(a))
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


# --- Advanced run switches (thinking, self-check, native tools, fix until cap, slots) --- #


def test_advanced_switches_save_and_reach_run(env, runs):
    body = {
        "local_thinking": "agent",
        "self_check": "local",
        "native_tools": "on",
        "fix_until_cap": "off",
        "max_slots": 3,
    }
    code, got = _call(env, "POST", "/api/settings", body)
    assert code == 200, got
    assert {k: got["settings"][k] for k in body} == body
    assert got["advanced"]["self_check"] == ["auto", "on", "local", "off"]
    p, kw = _build(env, runs)
    assert kw["local_thinking"] == "agent" and kw["self_check"] == "local"
    assert kw["native_tools"] is True and kw["fix_until_cap"] is False
    assert kw["max_slots"] == 3 and p.max_slots == 3
    # the floor's view carries them, so a restart re-sends the same switches
    assert server._build_kwargs(p)["self_check"] == "local"


def test_a_build_body_overrides_the_saved_switches(env, runs):
    assert _call(env, "POST", "/api/settings", {"local_thinking": "off"})[0] == 200
    _, kw = _build(env, runs, local_thinking="on", self_check="off")
    assert kw["local_thinking"] == "on" and kw["self_check"] == "off"


def test_auto_switches_are_not_sent_and_slots_follow_the_env(env, runs, monkeypatch):
    _, kw = _build(env, runs)
    for key in ("local_thinking", "self_check", "native_tools", "fix_until_cap"):
        assert key not in kw, key  # auto: run() falls back to the env, as the CLI does
    assert kw["max_slots"] == foundry.cli.AUTO_SLOTS_CAP
    monkeypatch.setenv("FOUNDRY_MAX_SLOTS", "5")
    _, kw = _build(env, runs)
    assert kw["max_slots"] == 5
    assert _call(env, "GET", "/api/settings")[1]["max_slots"] == {"max": ws.MAX_SLOTS, "default": 5}
    assert _call(env, "POST", "/api/settings", {"max_slots": 2})[0] == 200
    _, kw = _build(env, runs)
    assert kw["max_slots"] == 2  # the saved value wins over the env


@pytest.mark.parametrize(
    "field,value",
    [
        ("local_thinking", "maybe"),
        ("local_thinking", True),
        ("self_check", "agent"),
        ("native_tools", "local"),
        ("fix_until_cap", 1),
        ("max_slots", 0),
        ("max_slots", ws.MAX_SLOTS + 1),
        ("max_slots", 2.5),
        ("max_slots", True),
        ("max_slots", "many"),
    ],
)
def test_bad_advanced_switches_are_refused(env, runs, field, value):
    assert _call(env, "POST", "/api/settings", {field: value})[0] == 400
    assert not (env.tmp / "out" / "settings.json").exists()
    assert _call(env, "POST", "/api/projects", {"request": "x", field: value})[0] == 400
    assert runs == []


# --- defaults follow the env ----------------------------------------------------------- #


def test_local_defaults_follow_the_env(env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_LOCAL_URL", "http://10.0.0.5:1234/v1")
    monkeypatch.setenv("FOUNDRY_LOCAL_MODEL", "qwen/qwen3-coder-30b")
    got = _call(env, "GET", "/api/settings")[1]
    assert got["settings"]["local_url"] == "http://10.0.0.5:1234/v1"
    assert got["settings"]["local_model"] == "qwen/qwen3-coder-30b"
    assert got["presets"]["local"]["T1"]["model"] == "qwen/qwen3-coder-30b"
    # a saved value still wins over the env
    assert _call(env, "POST", "/api/settings", {"local_model": "llama/x-8b"})[0] == 200
    assert _call(env, "GET", "/api/settings")[1]["settings"]["local_model"] == "llama/x-8b"


def test_an_unusable_env_local_url_falls_back_to_the_default(env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_LOCAL_URL", "file:///etc/passwd")
    monkeypatch.setenv("FOUNDRY_LOCAL_MODEL", "not a model id")
    s = _call(env, "GET", "/api/settings")[1]["settings"]
    assert s["local_url"] == local_transport.DEFAULT_LOCAL_URL
    assert s["local_model"] == local_transport.DEFAULT_LOCAL_MODEL


# --- live progress lines (`log` events) -------------------------------------------------- #


def _fake_run(monkeypatch, lines: list[str], **result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    sig = inspect.signature(foundry.cli.run)

    def fake(*args: Any, **kwargs: Any) -> Any:
        sig.bind(*args, **kwargs)
        calls.append(kwargs)
        for line in lines:
            kwargs["log_cb"](line)
        out = {"passed": True, "phase": "passed", **result}
        return SimpleNamespace(mode="assistant", artifact_uri=None, gate_statuses={}, **out)

    monkeypatch.setattr(foundry.cli, "run", fake)
    return calls


def test_progress_lines_stream_as_log_events_with_model_calls_coalesced(env, monkeypatch):
    monkeypatch.setattr(server, "_LOG_CHATTY_GAP", 3600.0)
    _fake_run(
        monkeypatch,
        [
            "[08:00:00] start model call: planner (T1, ideation)",
            "[08:00:01] done  model call: planner in 89s, 20k tokens (T1)",
            "[08:00:02] start model call: coder (T2)",
            "[08:00:03] self-check T-ORDERS: red (4 own, 0 pre-existing line(s); 1.3s; 8 left)",
            "[08:00:03] self-check T-ORDERS: red (4 own, 0 pre-existing line(s); 1.3s; 8 left)",
            "[08:00:04] WARNING coder: context truncated: sent ~9000, model saw 4000 (cut)",
            "[08:00:05] retry model call: coder — timeout [1/3], waiting 0s",
            "[08:00:06] self-check T-ORDERS: green (0.8s; 7 left)",
            "",
        ],
    )
    code, got = _call(env, "POST", "/api/projects", {"request": "build a thing"})
    assert code == 200
    p = server._PROJECTS[got["id"]]
    _wait_ended(p)
    logs = [e for e in p.log if e["type"] == "log"]
    assert [(e["kind"], e.get("verdict")) for e in logs] == [
        ("call", None),  # the first model call; the next two are held back
        ("self-check", "red"),  # an immediate repeat collapses
        ("warning", None),
        ("retry", None),
        ("self-check", "green"),
    ]
    assert logs[0]["text"] == "start model call: planner (T1, ideation)"
    assert all(not e["text"].startswith("[") for e in logs)
    # durable: a viewer after a restart replays them too
    assert [e["type"] for e in env.store.events_of(p.id)].count("log") == len(logs)


def test_held_back_model_calls_are_counted_on_the_next_one():
    p = Project(id="t", request="r", mode="assistant", floor=1)
    assert server.take_log(p, "start model call: a", now=100.0) is not None
    assert server.take_log(p, "done model call: a in 1s", now=101.0) is None
    assert server.take_log(p, "start model call: b", now=102.0) is None
    assert server.take_log(p, "WARNING b: context truncated", now=102.5) is not None
    ev = server.take_log(p, "start model call: c", now=100.0 + server._LOG_CHATTY_GAP)
    assert ev is not None and ev["more"] == 2
    assert "more" not in (server.take_log(p, "x", now=200.0) or {})


# --- DRAFT-STATUS for a floor that did not ship ------------------------------------------ #


def _get_text(env: Any, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=30)
    conn.request("GET", path)
    resp = conn.getresponse()
    out = resp.read().decode("utf-8")
    conn.close()
    return resp.status, out


_DRAFT = "# Draft status\n\nBuilt: T1. Left: T2 (test gate red).\n"


def test_a_non_shipped_floor_shows_its_draft_status(env, monkeypatch):
    _fake_run(monkeypatch, [], passed=False, phase="halted", draft_status=_DRAFT)
    code, got = _call(env, "POST", "/api/projects", {"request": "build a thing"})
    assert code == 200
    p = server._PROJECTS[got["id"]]
    _wait_ended(p)
    assert p.draft_status == _DRAFT

    halt = _call(env, "GET", f"/api/projects/{p.id}/insight")[1]["halt"]
    assert halt["draft_status"] == _DRAFT
    code, files = _call(env, "GET", f"/api/files/{p.id}")
    assert code == 200 and files["draft"] is True
    assert {"path": "DRAFT-STATUS.md", "size": len(_DRAFT), "virtual": True} in files["files"]
    assert _get_text(env, f"/api/file/{p.id}?path=DRAFT-STATUS.md") == (200, _DRAFT)

    # A restart restores it from the floor's durable log.
    server._PROJECTS.clear()
    server._load_persisted()
    assert server._PROJECTS[p.id].draft_status == _DRAFT
    assert _get_text(env, f"/api/file/{p.id}?path=DRAFT-STATUS.md") == (200, _DRAFT)


def test_a_shipped_floor_has_no_draft_status(env, monkeypatch):
    _fake_run(monkeypatch, [], draft_status=_DRAFT)
    _, got = _call(env, "POST", "/api/projects", {"request": "build a thing"})
    p = server._PROJECTS[got["id"]]
    _wait_ended(p)
    assert p.draft_status == ""
    assert _get_text(env, f"/api/file/{p.id}?path=DRAFT-STATUS.md")[0] == 404


# --- resume with a different model setup ---------------------------------------------------- #


def _halted(env: Any, monkeypatch, runs: list[dict[str, Any]], **body: Any) -> Project:
    p, _ = _build(env, runs, **body)
    p.passed, p.phase = False, "halted"
    monkeypatch.setattr(server, "_has_durable_checkpoint", lambda _tid: True)
    return p


def test_resume_with_a_new_model_mode_runs_on_that_modes_levels(env, runs, monkeypatch):
    p = _halted(env, monkeypatch, runs, scope="demo")
    assert runs[-1]["model_mode"] == "cloud"
    code, got = _call(
        env,
        "POST",
        f"/api/projects/{p.id}/resume",
        {"model_mode": "local", "local_thinking": "agent", "max_slots": 2},
    )
    assert code == 200, got
    _wait_ended(p)
    kw = runs[-1]
    assert kw["model_mode"] == "local" and kw["levels"] is None  # the local preset
    assert kw["local_thinking"] == "agent" and kw["max_slots"] == 2
    assert kw["scope"] == "demo"  # the plan's scope is kept
    resumed = [e for e in p.log if e["type"] == "resumed"][-1]
    assert resumed["build"]["model_mode"] == "local" and resumed["slots"] == 2

    # A restart re-reads the model setup the latest resume set.
    q = Project(id="q", request="r", mode="assistant", floor=9, log=list(p.log))
    server._restore_controls(q)
    assert q.build["model_mode"] == "local" and q.max_slots == 2


def test_resume_keeps_an_edited_level_table_unless_the_mode_changes(env, runs, monkeypatch):
    preset = _call(env, "GET", "/api/settings")[1]["presets"]["cloud"]
    levels = {**preset, "T1": {"backend": "mock", "model": "custom-1"}}
    p = _halted(env, monkeypatch, runs, levels=levels)
    assert _call(env, "POST", f"/api/projects/{p.id}/resume", {"self_check": "off"})[0] == 200
    _wait_ended(p)
    assert runs[-1]["levels"]["T1"]["model"] == "custom-1"
    assert runs[-1]["self_check"] == "off"
    p.passed, p.phase = False, "halted"
    code, got = _call(env, "POST", f"/api/projects/{p.id}/resume", {"model_mode": "hybrid"})
    assert code == 200, got
    _wait_ended(p)
    assert runs[-1]["model_mode"] == "hybrid" and runs[-1]["levels"] is None


@pytest.mark.parametrize(
    "body,why",
    [
        ({"scope": "standard"}, "scope"),
        ({"spec": "# new spec"}, "spec"),
        ({"profile": "go"}, "profile"),
        ({"model_mode": "turbo"}, "model_mode"),
        ({"local_url": "ftp://x"}, "URL"),
        ({"self_check": "maybe"}, "self_check"),
        ({"max_slots": 0}, "max_slots"),
    ],
)
def test_resume_refuses_plan_changes_and_bad_settings(env, runs, monkeypatch, body, why):
    p = _halted(env, monkeypatch, runs)
    before = dict(p.build)
    code, got = _call(env, "POST", f"/api/projects/{p.id}/resume", body)
    assert code == 400 and why in got["error"], got
    assert p.build == before and p.status == "done" and len(runs) == 1
