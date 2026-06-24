"""Alternative configured backends (proxy/cli_transports) — fakes only.

Proves the SAME proxy flow runs on claude -p / codex / ollama-gemma with the
backend config swapped: lineage is proxy-observed (not self-reported), the
verdict path still parses + stamps a verifiable binding, an unobservable model
fails closed, and a backend failure surfaces as the proxy's fail-closed
ProxyUnavailable (no family fallback). No real binary or network is used.
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
    ClaudeCliTransport,
    CliResult,
    CodexCliTransport,
    OllamaTransport,
    backend_from_env,
)
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq

KEY = b"dev-zone-b-cli-key"
KEY_ID = "dev-key-cli"


def _signer() -> ZoneBSigner:
    return ZoneBSigner(key=KEY, key_id=KEY_ID)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


def _claude_runner(payload: dict[str, Any], *, returncode: int = 0) -> Any:
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        return CliResult(returncode, json.dumps(payload))

    run.calls = []  # type: ignore[attr-defined]
    return run


def _codex_runner(lines: list[dict[str, Any]], *, returncode: int = 0) -> Any:
    def run(argv: Sequence[str], timeout_s: float | None) -> CliResult:
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        return CliResult(returncode, "\n".join(json.dumps(line) for line in lines))

    run.calls = []  # type: ignore[attr-defined]
    return run


def _ollama_post(response: dict[str, Any]) -> Any:
    def post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        post.calls.append((url, payload))  # type: ignore[attr-defined]
        return response

    post.calls = []  # type: ignore[attr-defined]
    return post


# --------------------------------------------------------------------------- #
# claude -p                                                                    #
# --------------------------------------------------------------------------- #


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
# codex exec                                                                   #
# --------------------------------------------------------------------------- #


def test_codex_completion_observes_openai_lineage() -> None:
    runner = _codex_runner(
        [
            {"type": "session_configured", "model": "gpt-5-codex"},
            {"msg": {"type": "agent_message", "message": "codex says hi"},
             "usage": {"input_tokens": 7, "output_tokens": 3}},
        ]
    )
    proxy = build_backend("codex", runner=runner)
    result = proxy.complete(
        "n1", "T1", "SOP", [{"role": "user", "content": "go"}], purpose="ideation"
    )
    assert result.text == "codex says hi"
    assert result.served_model == "gpt-5-codex"
    # A genuinely DIFFERENT served_family than the Anthropic default.
    assert result.served_family == frozenset({"openai-gpt"})
    assert result.usage["input_tokens"] == 7
    argv = runner.calls[0]
    assert argv[:2] == ["codex", "exec"] and "-m" in argv and "gpt-5-codex" in argv


def test_codex_default_tier_models_are_openai() -> None:
    proxy = build_backend("codex", runner=_codex_runner([]))
    assert proxy.tier_models["T1"] == "gpt-5-codex"


def test_codex_gate_call_pins_effort_and_never_a_temperature() -> None:
    # BLOCKER fix (arch §8): a gate/judge call on the Codex backend must pin a
    # determinism control the backend actually honours. gpt-* / codex-* are
    # reasoning models — they REJECT temperature; the Codex CLI forwards
    # `model_reasoning_effort`. The proxy must therefore pin effort, NOT a
    # temperature that the transport would silently drop (= no determinism).
    runner = _codex_runner(
        [
            {"type": "session_configured", "model": "gpt-5-codex"},
            {"msg": {"type": "agent_message", "message": "ok"}},
        ]
    )
    proxy = build_backend("codex", runner=runner)
    result = proxy.complete(
        "gate_node", "T1", "SOP", [{"role": "user", "content": "review"}], purpose="gate"
    )
    # Manifest records effort (the substitution), never a temperature.
    assert result.manifest_record["sampling"] == {"effort": "high"}
    assert "sampling_note" in result.manifest_record
    # The effort control reached the CLI as model_reasoning_effort; no temperature.
    argv = runner.calls[0]
    assert "-c" in argv and "model_reasoning_effort=high" in argv
    assert not any("temperature" in str(a) for a in argv)


def test_codex_t2_gate_call_also_pins_determinism() -> None:
    # gpt-5 (T2) is still a reasoning model => effort, not a dropped temperature.
    runner = _codex_runner(
        [
            {"type": "session_configured", "model": "gpt-5"},
            {"msg": {"type": "agent_message", "message": "ok"}},
        ]
    )
    proxy = build_backend("codex", runner=runner)
    result = proxy.complete(
        "gate_node", "T2", "SOP", [{"role": "user", "content": "review"}], purpose="gate"
    )
    assert result.manifest_record["sampling"] == {"effort": "high"}
    assert "model_reasoning_effort=high" in runner.calls[0]


# --------------------------------------------------------------------------- #
# ollama gemma                                                                 #
# --------------------------------------------------------------------------- #


def test_ollama_gemma_completion_observes_gemma_lineage() -> None:
    post = _ollama_post(
        {
            "model": "gemma3:27b",
            "message": {"role": "assistant", "content": "gemma output"},
            "done_reason": "stop",
            "prompt_eval_count": 12,
            "eval_count": 4,
        }
    )
    proxy = build_backend("ollama-gemma", http_post=post)
    result = proxy.complete(
        "n1", "T1", "SYS", [{"role": "user", "content": "hi"}], purpose="ideation"
    )
    assert result.text == "gemma output"
    assert result.served_model == "gemma3:27b"
    assert result.served_family == frozenset({"google-gemma"})
    assert result.usage["output_tokens"] == 4
    # System block + user message went as real chat roles (no flattening).
    _url, payload = post.calls[0]
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user"]
    assert payload["model"] == "gemma3:27b"


def test_ollama_verdict_channel_sends_native_json_schema_format() -> None:
    post = _ollama_post(
        {
            "model": "gemma3:27b",
            "message": {"content": json.dumps({"status": "approve", "findings": []})},
        }
    )
    proxy = build_backend("ollama-gemma", http_post=post, signer=_signer())
    seq = Seq("r", 1, 1, "review", 0)
    jr = proxy.judge(
        "review",
        "cas://sha256/abc",
        "sha",
        "nonce",
        "SOP",
        [{"role": "user", "content": "x"}],
        seq=seq,
    )
    assert jr.gate_result.status is GateStatus.PASS
    # Ollama keeps the grammar: the proxy's verdict schema rode in `format`.
    _url, payload = post.calls[0]
    assert payload["format"]["properties"]["status"]["enum"] == ["approve", "request-changes"]
    # Deterministic judge call pinned temperature 0 (gemma is a sampling model).
    assert payload["options"]["temperature"] == 0


def test_ollama_gate_call_pins_temperature_zero_not_effort() -> None:
    # Gemma honours options.temperature, so a deterministic gate call pins
    # temperature 0 (NOT an effort control it would ignore).
    post = _ollama_post(
        {"model": "gemma3:27b", "message": {"content": "ok"}}
    )
    proxy = build_backend("ollama-gemma", http_post=post)
    result = proxy.complete(
        "gate_node", "T1", "SYS", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.manifest_record["sampling"] == {"temperature": 0}
    assert "sampling_note" not in result.manifest_record
    _url, payload = post.calls[0]
    assert payload["options"]["temperature"] == 0
    assert "format" not in payload  # no schema on a plain gate complete()


def test_ollama_transport_requires_pinned_model() -> None:
    with pytest.raises(TransportError):
        OllamaTransport(http_post=_ollama_post({})).send({"messages": []})


# --------------------------------------------------------------------------- #
# config-driven selection                                                      #
# --------------------------------------------------------------------------- #


def test_backend_from_env_defaults_to_anthropic() -> None:
    proxy = backend_from_env({})
    assert proxy.tier_models["T1"] == "claude-opus-4-8"


def test_backend_from_env_selects_named_backend() -> None:
    proxy = backend_from_env({"FOUNDRY_MODEL_BACKEND": "ollama-gemma"}, http_post=_ollama_post({}))
    assert proxy.tier_models["T1"] == "gemma3:27b"


def test_build_backend_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        build_backend("bedrock")  # type: ignore[arg-type]


def test_codex_transport_direct_construction() -> None:
    t = CodexCliTransport(runner=_codex_runner([{"type": "agent_message", "text": "x"}]))
    resp = t.send({"model": "gpt-5", "messages": [{"role": "user", "content": "q"}]})
    assert resp.text == "x"
