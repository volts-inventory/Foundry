"""CLI ``--spec`` / ``run(spec=)`` — seed a full design-spec doc into a build.

(EXECUTION-PLAN.md §4 [8])

A build may start from a real design-spec document (a ``.md``/``.txt`` FILE, a
DIRECTORY of them, or literal text) in ADDITION to the one-line ``request``. The
spec text is seeded onto the ``spec`` state channel, where the Stakeholder/Client
Proxy and Product Manager GROUND the IntentBrief/PRD in it (so the product —
and, through its outputs, the architecture — layer builds from the provided spec
rather than synthesizing from a one-liner). The spec is plain INPUT DATA: it
INFORMS the criteria but never bypasses the acceptance-criteria derivation or any
signed gate, and request-only runs are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.agents.product.product_manager import _intent_text
from foundry.agents.product.stakeholder_proxy import _intake_text
from foundry.cli import _load_spec, main, run
from foundry.proxy.test_doubles import MockTransport

# A multi-paragraph design spec carrying a distinctive marker the test can look
# for in the prompts the product nodes send through the proxy.
_SPEC_TEXT = (
    "# Link Shortener — Design Spec\n\n"
    "## Goal\nProvide a SPEC_MARKER_7F3 short-link service with analytics.\n\n"
    "## Requirements\n- Create short codes\n- Redirect with 301\n- Rate-limit abuse\n"
)


# ---- the product layer reads the spec off the channel (the seam) ------------


def test_intake_text_grounds_in_spec_when_present() -> None:
    """The Stakeholder/Client Proxy intake includes the spec when one is seeded."""
    state = {"request": "build a link shortener", "spec": _SPEC_TEXT}
    text = _intake_text(state)  # type: ignore[arg-type]
    assert "build a link shortener" in text  # request still summarizes intent
    assert "SPEC_MARKER_7F3" in text  # spec grounds the detail
    assert "DESIGN SPEC" in text


def test_pm_intent_text_grounds_in_spec_when_present() -> None:
    state = {"request": "build a link shortener", "spec": _SPEC_TEXT}
    text = _intent_text(state)  # type: ignore[arg-type]
    assert "SPEC_MARKER_7F3" in text
    assert "DESIGN SPEC" in text


def test_intake_text_request_only_unchanged() -> None:
    """No spec channel -> the intake is EXACTLY the raw request (unchanged)."""
    state = {"request": "build a link shortener"}
    assert _intake_text(state) == "build a link shortener"  # type: ignore[arg-type]
    assert _intent_text(state) == "build a link shortener"  # type: ignore[arg-type]


# ---- _load_spec resolves file / dir / literal text / list -------------------


def test_load_spec_reads_file(tmp_path: Path) -> None:
    f = tmp_path / "spec.md"
    f.write_text(_SPEC_TEXT, encoding="utf-8")
    assert "SPEC_MARKER_7F3" in _load_spec(str(f))


def test_load_spec_reads_directory(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\nSPEC_MARKER_7F3 part one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("part two\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("ignored = 1\n", encoding="utf-8")
    text = _load_spec(str(tmp_path))
    assert "SPEC_MARKER_7F3" in text
    assert "part two" in text
    assert "ignored = 1" not in text  # only .md/.txt are read


def test_load_spec_list_of_paths(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("alpha SPEC_MARKER_7F3\n", encoding="utf-8")
    b.write_text("beta\n", encoding="utf-8")
    text = _load_spec([str(a), str(b)])
    assert "alpha" in text and "beta" in text


def test_load_spec_literal_text_passthrough() -> None:
    assert _load_spec("just literal intent text") == "just literal intent text"


def test_load_spec_empty_string_is_no_spec_not_cwd() -> None:
    # FOOT-GUN GUARD: Path("").exists() is True (resolves to cwd), so an explicit
    # empty/blank spec must short-circuit to "" rather than ingesting the entire
    # working directory's .md/.txt files.
    assert _load_spec("") == ""
    assert _load_spec("   ") == ""
    assert _load_spec("\n\t") == ""


def test_load_spec_missing_path_in_list_raises() -> None:
    # A path given via the list form (as the CLI --spec flag does) must exist.
    with pytest.raises(FileNotFoundError):
        _load_spec(["/nonexistent/does-not-exist-xyz.md"])


def test_load_spec_empty_dir_raises(tmp_path: Path) -> None:
    (tmp_path / "notes.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"no .* files"):
        _load_spec(str(tmp_path))


def test_load_spec_caps_large_text() -> None:
    from foundry.cli import _SPEC_MAX_BYTES
    huge = "x" * (_SPEC_MAX_BYTES * 2)
    out = _load_spec(huge)
    assert len(out.encode("utf-8")) <= _SPEC_MAX_BYTES


def test_load_spec_caps_large_multibyte_text_on_bytes() -> None:
    """The cap is on ENCODED BYTES, so a multibyte spec is bounded too (not just
    ASCII) — and the truncation never leaves a partial UTF-8 char."""
    from foundry.cli import _SPEC_MAX_BYTES
    huge = "€" * _SPEC_MAX_BYTES  # 3 bytes each -> ~3x the cap
    out = _load_spec(huge)
    assert len(out.encode("utf-8")) <= _SPEC_MAX_BYTES  # truly byte-bounded
    out.encode("utf-8")  # decodes cleanly (no partial trailing char)


# ---- end-to-end: the product stages SEE the spec during a real run ----------


def _install_request_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every request the CLI's transport sends.

    ``MockTransport`` already records each outgoing request on ``.requests``; we
    only need a handle to the instance ``run`` builds internally, so we wrap
    ``MockTransport.scripted`` to stash it on a shared list.
    """
    captured: list[dict[str, Any]] = []
    real_scripted = MockTransport.scripted.__func__  # type: ignore[attr-defined]

    def _capturing_scripted(mapping: dict[str, str], **kwargs: Any) -> MockTransport:
        tx = real_scripted(MockTransport, mapping, **kwargs)
        captured.append({"_tx": tx})
        return tx

    monkeypatch.setattr(MockTransport, "scripted", staticmethod(_capturing_scripted))
    return captured


def _seen_prompts(captured: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for entry in captured:
        tx = entry["_tx"]
        out.extend(json.dumps(r, ensure_ascii=False) for r in tx.requests)
    return out


def test_run_with_spec_completes_and_product_sees_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing build seeded with a --spec file completes AND the spec text is
    seen by the product-layer model calls (Stakeholder + Product Manager)."""
    captured = _install_request_capture(monkeypatch)
    spec_file = tmp_path / "design.md"
    spec_file.write_text(_SPEC_TEXT, encoding="utf-8")

    result = run(
        "build a link shortener",
        spec=str(spec_file),
        thread_id="cli-spec-pass",
    )
    assert result.passed is True
    spec_status = result.gate_statuses.get("spec")
    assert spec_status is not None and spec_status.value == "pass"

    # The spec text reached the product layer's prompts (the grounding seam).
    spec_grounded = [r for r in _seen_prompts(captured) if "SPEC_MARKER_7F3" in r]
    assert spec_grounded, "spec text never reached any model call"


def test_run_request_only_does_not_inject_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request-only run still works AND no spec marker leaks into any prompt."""
    captured = _install_request_capture(monkeypatch)
    result = run("build a link shortener", thread_id="cli-spec-none")
    assert result.passed is True
    # No spec content leaks, and the product nodes do NOT prepend the
    # request/spec body framing they only add when a spec is seeded.
    seen = _seen_prompts(captured)
    assert not any("SPEC_MARKER_7F3" in r for r in seen)
    assert not any("REQUEST (intent summary)" in r for r in seen)


# ---- CLI --spec flag parsing ------------------------------------------------


def test_main_spec_flag_runs(tmp_path: Path) -> None:
    spec_file = tmp_path / "design.md"
    spec_file.write_text(_SPEC_TEXT, encoding="utf-8")
    code = main(["--spec", str(spec_file), "build a link shortener"])
    assert code == 0


def test_main_spec_flag_repeatable(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("# A\nfirst\n", encoding="utf-8")
    b.write_text("# B\nsecond\n", encoding="utf-8")
    code = main(["--spec", str(a), "--spec", str(b), "build a link shortener"])
    assert code == 0


def test_main_spec_only_no_request_runs(tmp_path: Path) -> None:
    # A spec FILE can stand in for the one-line request — `--spec FILE` with no
    # positional request still builds (the request is derived from the spec).
    spec_file = tmp_path / "design.md"
    spec_file.write_text(_SPEC_TEXT, encoding="utf-8")
    assert main(["--spec", str(spec_file)]) == 0


def test_request_from_spec_uses_first_heading() -> None:
    from foundry.cli import _request_from_spec
    assert _request_from_spec(_SPEC_TEXT) == "Link Shortener — Design Spec"
    assert _request_from_spec("\n\n  \n") == "Build the system described in the provided spec."


def test_main_no_args_is_usage_error() -> None:
    # Neither a request nor a spec → usage error (exit 2).
    assert main([]) == 2


def test_main_spec_flag_requires_argument() -> None:
    assert main(["--spec"]) == 2


def test_main_spec_missing_file_errors_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.md"
    code = main(["--spec", str(missing), "build a link shortener"])
    assert code == 2
    err = capsys.readouterr().err
    assert "spec path not found" in err
