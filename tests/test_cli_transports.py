"""The claude -p configured backend (proxy/cli_transports) — fakes only.

Proves the proxy flow runs on the claude -p backend: lineage is proxy-observed
(not self-reported), the verdict path still parses + stamps a verifiable
binding, an unobservable model fails closed, and a backend failure surfaces as
the proxy's fail-closed ProxyUnavailable (no family fallback). No real binary or
network is used.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import (
    ProxyUnavailable,
    TransportError,
    UnknownModelLineageError,
    build_backend,
)
from foundry.proxy.cli_transports import (
    _CLAUDE_CAP_CACHE,
    ClaudeCliTransport,
    CliResult,
    reset_claude_cap_cache,
)
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq

KEY = b"dev-zone-b-cli-key"
KEY_ID = "dev-key-cli"


def _signer() -> ZoneBSigner:
    return ZoneBSigner(key=KEY, key_id=KEY_ID)


@pytest.fixture(autouse=True)
def _claude_cap_cache_isolation():
    """Prompt caching is always-on and probes `claude --version` once per binary.
    Pre-seed the default `claude` bin as capable so unrelated claude-cli tests don't
    see the probe consume a `runner.calls[0]` slot; tests that exercise the probe use
    a distinct bin. Reset around every test so nothing leaks."""
    reset_claude_cap_cache()
    _CLAUDE_CAP_CACHE["claude"] = True
    yield
    reset_claude_cap_cache()


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


def _claude_runner(payload: dict[str, Any], *, returncode: int = 0) -> Any:
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        return CliResult(returncode, json.dumps(payload))

    run.calls = []  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# claude -p                                                                    #
# --------------------------------------------------------------------------- #


def _timeout_capturing_runner() -> Any:
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.timeouts.append(timeout_s)  # type: ignore[attr-defined]
        return CliResult(0, json.dumps({"type": "result", "subtype": "success",
                                        "result": "ok", "session_id": "s",
                                        "modelUsage": {"claude-opus-4-8": {}}}))
    run.timeouts = []  # type: ignore[attr-defined]
    return run


def test_cli_backends_apply_a_default_subprocess_timeout() -> None:
    # H4: a hung claude must not wedge the worker forever. The shipped backend
    # sets a non-None wall-clock cap by default, overridable.
    runner = _timeout_capturing_runner()
    proxy = build_backend("claude-cli", runner=runner)
    proxy.complete("n1", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="ideation")
    assert runner.timeouts[0] == 300.0                       # default cap applied

    over = _timeout_capturing_runner()
    proxy2 = build_backend("claude-cli", runner=over, timeout_s=42.0)
    proxy2.complete("n1", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="ideation")
    assert over.timeouts[0] == 42.0                          # override flows through


def test_claude_cli_completion_observes_model_and_flattens_prompt() -> None:
    runner = _claude_runner(
        {
            "type": "result",
            "subtype": "success",
            "result": "hello from claude",
            "session_id": "sess-123",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
            "modelUsage": {"claude-opus-4-8": {"inputTokens": 10, "outputTokens": 5}},
        }
    )
    proxy = build_backend("claude-cli", runner=runner)
    result = proxy.complete(
        "n1", "T1", "SYS-SOP", [{"role": "user", "content": "do the thing"}], purpose="ideation"
    )
    assert result.text == "hello from claude"
    # Lineage is proxy-OBSERVED from modelUsage, resolved to the curated family.
    assert result.served_model == "claude-opus-4-8"
    assert result.served_family == frozenset({"anthropic-claude"})
    assert result.request_id == "claude-cli:sess-123"
    assert result.usage["input_tokens"] == 10
    # Prompt was flattened: pinned model + the SOP and user text reach the CLI.
    argv = runner.calls[0]
    assert "--model" in argv and "claude-opus-4-8" in argv
    joined = "\n".join(argv)
    assert "SYS-SOP" in joined and "do the thing" in joined


def _versioned_claude_runner(version: str, payload: dict[str, Any]) -> Any:
    """Fake `claude` that answers `--version` with a version banner and every other
    argv with the completion `payload` (so one runner serves the probe + the call)."""
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        if "--version" in argv:
            return CliResult(0, f"{version} (Claude Code)")
        return CliResult(0, json.dumps(payload))

    run.calls = []  # type: ignore[attr-defined]
    return run


def _claude_request(sop: str, user: str, model: str = "claude-opus-4-8") -> dict[str, Any]:
    """A request in the shape the proxy hands the transport (system as a cached
    text block, volatile content in messages)."""
    return {
        "model": model,
        "system": [{"type": "text", "text": sop, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }


def test_claude_cli_always_routes_sop_through_append_flag_for_caching() -> None:
    """Prompt caching is ALWAYS ON (not a setting): the stable SOP rides on
    --append-system-prompt (so Claude Code prompt-caches it) and is kept OUT of the
    -p user string, on a new-enough binary. The version probe runs once per binary."""
    payload = {"type": "result", "subtype": "success", "result": "ok",
               "modelUsage": {"claude-opus-4-8": {"inputTokens": 1, "outputTokens": 1}}}
    runner = _versioned_claude_runner("2.5.1", payload)
    tx = ClaudeCliTransport(claude_bin="claude-modern", runner=runner)

    tx.send(_claude_request("STABLE-SOP", "volatile"))
    tx.send(_claude_request("STABLE-SOP", "again"))

    version_calls = [c for c in runner.calls if "--version" in c]
    assert len(version_calls) == 1  # probed once, cached thereafter
    call = next(c for c in runner.calls if "-p" in c)
    assert "--append-system-prompt" in call
    assert call[call.index("--append-system-prompt") + 1] == "STABLE-SOP"
    prompt = call[call.index("-p") + 1]
    assert "STABLE-SOP" not in prompt and "volatile" in prompt


def test_claude_cli_rejects_a_binary_too_old_for_append_system_prompt() -> None:
    """A `claude` below the floor fails closed with an upgrade instruction rather
    than silently dropping caching — the minimum version is a hard REQUIREMENT."""
    payload = {"type": "result", "subtype": "success", "result": "ok",
               "modelUsage": {"claude-opus-4-8": {"inputTokens": 1, "outputTokens": 1}}}
    runner = _versioned_claude_runner("0.2.9", payload)
    tx = ClaudeCliTransport(claude_bin="claude-ancient", runner=runner)

    with pytest.raises(TransportError) as exc:
        tx.send(_claude_request("STABLE-SOP", "x"))
    assert "upgrade" in str(exc.value).lower() or "too old" in str(exc.value).lower()


def test_claude_cli_verdict_path_parses_and_stamps_verifiable_binding() -> None:
    verdict = json.dumps({"status": "request-changes", "findings": ["nit"]})
    runner = _claude_runner(
        {
            "subtype": "success",
            "result": verdict,
            "modelUsage": {"claude-opus-4-8": {"outputTokens": 9}},
        }
    )
    proxy = build_backend("claude-cli", runner=runner, signer=_signer())
    seq = Seq("r", 1, 1, "review", 0)
    jr = proxy.judge(
        "review",
        "cas://sha256/abc",
        "state-sha",
        "nonce-1",
        "SOP",
        [{"role": "user", "content": "x"}],
        seq=seq,
    )
    assert jr.gate_result.status is GateStatus.FAIL
    assert jr.gate_result.judgment_provenance is JudgmentProvenance.SELF
    # The binding the proxy stamped (outside the CLI) verifies.
    verifier = DevGateWriteVerifier(KEY, KEY_ID, GlobalRecords())
    assert verifier.verify_judgment_binding(jr.gate_result)


def test_claude_cli_unobservable_model_fails_closed() -> None:
    # No modelUsage and no top-level model => empty observation => fail closed.
    runner = _claude_runner({"subtype": "success", "result": "text-but-no-model"})
    proxy = build_backend("claude-cli", runner=runner)
    with pytest.raises(UnknownModelLineageError):
        proxy.complete("n1", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="ideation")


def test_claude_cli_error_surfaces_as_proxy_unavailable() -> None:
    def boom(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        return CliResult(1, "", "command not found")

    proxy = build_backend("claude-cli", runner=boom)
    # Bounded retry then fail-closed — never a silent family fallback.
    with pytest.raises(ProxyUnavailable):
        proxy.complete("n1", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="ideation")


def test_claude_cli_transport_raises_transport_error_on_timeout() -> None:
    def slow(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        return CliResult(124, "", "", timed_out=True)

    with pytest.raises(TransportError):
        ClaudeCliTransport(runner=slow).send({"model": "claude-opus-4-8", "messages": []})


# --------------------------------------------------------------------------- #
# config-driven selection                                                      #
# --------------------------------------------------------------------------- #


def test_build_backend_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        build_backend("bedrock")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Parser hardening (task [6]) — current/legacy shapes + fail-closed-on-empty   #
# --------------------------------------------------------------------------- #


def _raw_runner(stdout: str, *, returncode: int = 0) -> Any:
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        return CliResult(returncode, stdout)

    run.calls = []  # type: ignore[attr-defined]
    return run


def test_claude_real_shape_fixture_result() -> None:
    from tests.fixtures.cli_transport_samples import CLAUDE_P_JSON_RESULT

    runner = _raw_runner(CLAUDE_P_JSON_RESULT)
    resp = ClaudeCliTransport(runner=runner).send(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "q"}]}
    )
    assert resp.text == "Here is the final answer from claude."
    assert resp.served_model == "claude-opus-4-8"
    assert resp.usage["input_tokens"] == 140
    assert resp.usage["output_tokens"] == 52
    assert resp.usage["cache_read_input_tokens"] == 96
    assert resp.request_id == "claude-cli:a1b2c3d4-0000-4444-8888-abcdef012345"


def test_claude_envelope_without_modelusage_observes_top_level_model() -> None:
    # Older/degraded envelope: no modelUsage, but total_cost_usd + usage + a
    # top-level model. The model is still OBSERVED (not guessed), so this does
    # not fail closed.
    from tests.fixtures.cli_transport_samples import CLAUDE_P_JSON_NO_MODELUSAGE

    runner = _raw_runner(CLAUDE_P_JSON_NO_MODELUSAGE)
    resp = ClaudeCliTransport(runner=runner).send(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "q"}]}
    )
    assert resp.served_model == "claude-opus-4-8"
    assert resp.text == "Answer without modelUsage."
    assert resp.usage["input_tokens"] == 20
