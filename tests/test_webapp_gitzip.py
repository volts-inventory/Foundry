"""The Workshop's git export: a floor lineage becomes ONE git repo (r01..rNN
tagged commits) via the same deterministic export_lineage the CLI uses."""

from __future__ import annotations

from typing import Any

import pytest

import webapp.server as server


def test_gitzip_builds_tagged_lineage(monkeypatch: Any, tmp_path: Any) -> None:
    import io
    import zipfile

    monkeypatch.setattr(server, "_lineage_ids", lambda pid: ["a", "b"])
    trees = {"a": {"app.py": b"V = 1\n"}, "b": {"app.py": b"V = 2\n", "farm.py": b"F\n"}}
    monkeypatch.setattr(server, "_overlay_tree", lambda pid: trees[pid])
    monkeypatch.setattr(server._STORE, "get", lambda pid: {"request": f"req-{pid}"})

    sent: dict[str, Any] = {}

    class H:  # duck-typed handler: only what _gitzip touches
        _json = staticmethod(lambda code, obj: sent.update(json=(code, obj)))
        def send_response(self, code): sent["code"] = code
        def send_header(self, k, v): sent[k] = v
        def end_headers(self): pass
        class wfile:  # noqa: N801
            @staticmethod
            def write(b): sent["body"] = b

    server.Handler._gitzip(H(), "b")
    assert sent.get("code") == 200, sent
    zf = zipfile.ZipFile(io.BytesIO(sent["body"]))
    names = zf.namelist()
    assert any(n.startswith(".git/") for n in names)  # a REAL git repo
    assert zf.read("app.py") == b"V = 2\n"  # HEAD tree = latest release
    assert zf.read("farm.py") == b"F\n"


def test_gitzip_no_lineage_404(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_lineage_ids", lambda pid: [])
    sent: dict[str, Any] = {}

    class H:
        _json = staticmethod(lambda code, obj: sent.update(json=(code, obj)))

    server.Handler._gitzip(H(), "x")
    assert sent["json"][0] == 404
