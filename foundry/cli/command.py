"""The command line: flag parsing, usage text, and :func:`main`."""

from __future__ import annotations

import json
import os
import sys

from foundry.agents.implementation import DEFAULT_WORKERS
from foundry.cli.defaults import (
    _MOCK_BACKEND,
    ADAPTERS,
    BACKENDS,
    MODES,
    SCOPES,
    env_local_model,
    env_local_url,
    env_max_slots,
)
from foundry.cli.result import _TRUST_POSTURE
from foundry.cli.runner import run
from foundry.cli.spec import _load_spec, _request_from_spec
from foundry.cli.wiring import _TIER_ENV
from foundry.graph.supervisor import MODE_ASSISTANT
from foundry.modes import MODEL_MODES
from foundry.proxy.local_transport import check_local_server
from foundry.proxy.token_meter import RunCapExceeded, TimeCapExceeded
from foundry.schemas.implementation import DEFAULT_TICKET_TIERS, Difficulty, Specialty

# ---- entry point ------------------------------------------------------------


# Flags whose features were removed: reject them rather than letting the leftover
# tokens silently become part of the build request.
_REMOVED_FLAGS = frozenset(
    {"--packs", "--apply", "--guardian-approve", "--strict", "--docs", "--checkpoints"}
)


def _request_text(args: list[str]) -> str:
    removed = [a.split("=", 1)[0] for a in args if a.split("=", 1)[0] in _REMOVED_FLAGS]
    if args and args[0] in ("evolve", "milestones", "autopilot"):
        removed.insert(0, f"the {args[0]} subcommand")
    if removed:
        raise _CliUsageError(f"{removed[0]} is no longer supported")
    return " ".join(args).strip()


class _CliUsageError(Exception):
    """A flag-parsing usage error; ``main`` prints ``error: <msg>`` and returns 2."""


def _take_value(args: list[str], flag: str, *, what: str) -> tuple[list[str], str | None]:
    """Pop ``flag VALUE`` out of ``args``; return ``(remaining, value)`` — or
    ``(args, None)`` if the flag is absent. Raises :class:`_CliUsageError` if the
    flag is present with no following value. (Replaces the 13 copy-pasted
    ``if flag in args: i = args.index(flag); …`` blocks; H5.)"""
    if flag not in args:
        return args, None
    i = args.index(flag)
    if i + 1 >= len(args):
        raise _CliUsageError(f"{flag} requires {what}")
    return args[:i] + args[i + 2 :], args[i + 1]


def _kv_pairs(text: str, what: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in (p.strip() for p in text.split(",")):
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{what}: expected key=value, got {item!r}")
        pairs.append((key.strip().lower(), value.strip()))
    return pairs


def parse_workers(text: str) -> dict[str, int]:
    """``frontend=2,backend=3,data=1,test=2`` -> per-specialty ticket limits, over
    :data:`DEFAULT_WORKERS`. Unknown specialty or a value below 1 is a ValueError."""
    known = sorted(s.value for s in Specialty)
    out = dict(DEFAULT_WORKERS)
    for key, value in _kv_pairs(text, "workers"):
        if key not in out:
            raise ValueError(f"workers: unknown specialty {key!r}; choose from {', '.join(known)}")
        if not value.isdigit() or int(value) < 1:
            raise ValueError(f"workers: {key} needs a whole number >= 1, got {value!r}")
        out[key] = int(value)
    return out


def parse_ticket_tiers(text: str) -> dict[Difficulty, str]:
    """``trivial=T3,standard=T2,complex=T1`` -> difficulty -> model tier, over the
    defaults. Unknown difficulty or tier is a ValueError."""
    out = dict(DEFAULT_TICKET_TIERS)
    for key, value in _kv_pairs(text, "ticket tiers"):
        try:
            difficulty = Difficulty(key)
        except ValueError:
            raise ValueError(
                f"ticket tiers: unknown difficulty {key!r}; choose from "
                f"{', '.join(d.value for d in Difficulty)}"
            ) from None
        tier = value.upper()
        if tier not in _TIER_ENV:
            raise ValueError(
                f"ticket tiers: unknown tier {value!r}; choose from {', '.join(_TIER_ENV)}"
            )
        out[difficulty] = tier
    return out


def _take_int(
    args: list[str], flag: str, *, minimum: int | None = None
) -> tuple[list[str], int | None]:
    """Pop an integer-valued ``flag``. Raises :class:`_CliUsageError` on a missing
    value, a non-integer, or a value below ``minimum``."""
    args, raw = _take_value(args, flag, what="an integer")
    if raw is None:
        return args, None
    try:
        value = int(raw)
    except ValueError as exc:
        raise _CliUsageError(f"{flag} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise _CliUsageError(f"{flag} must be >= {minimum}")
    return args, value


def main(argv: list[str] | None = None) -> int:
    """``python -m foundry.cli "build a ..."`` — run the smoke path, print a report.

    Returns process exit code 0 on a clean Spec/Build/Test PASS (a PR action was
    emitted), 1 on a halted/failed run, 2 on a usage error, 3 when --token-cap or
    --time-cap stopped the run. ``--local-check`` reports the local model server
    and exits 0 when it is reachable (and serves --local-model), else 1.
    """
    args = sys.argv[1:] if argv is None else argv
    # Simple value flags parse through `_take_value`/`_take_int` (H5: one helper
    # instead of 13 copy-pasted index/slice blocks); a usage error raises and is
    # rendered identically below. Comments mark the non-obvious semantics.
    try:
        args, output_dir = _take_value(args, "--out", what="a directory argument")
        # --base DIR: change that shipped build; the request is the change.
        args, base_dir = _take_value(args, "--base", what="a directory argument")

        # --mode assistant: the only mode; the flag is accepted for compatibility.
        args, _mode = _take_value(args, "--mode", what="a value")
        mode = _mode if _mode is not None else MODE_ASSISTANT
        if mode not in MODES:
            raise _CliUsageError(f"unknown --mode {mode!r}; choose one of {', '.join(MODES)}")

        # --backend default is FOUNDRY_MODEL_BACKEND if set, else "mock" (keyless,
        # deterministic — keeps the no-credential quickstart + hermetic suite
        # byte-identical). A deployment sets the env once; an explicit flag overrides.
        backend = os.environ.get("FOUNDRY_MODEL_BACKEND", _MOCK_BACKEND) or _MOCK_BACKEND
        if backend not in BACKENDS:
            backend = _MOCK_BACKEND
        args, _backend = _take_value(args, "--backend", what="a value")
        if _backend is not None:
            if _backend not in BACKENDS:
                raise _CliUsageError(
                    f"unknown --backend {_backend!r}; choose one of {', '.join(BACKENDS)}"
                )
            backend = _backend

        # --state-dir DIR: durable file/volume-backed state (overrides
        # FOUNDRY_STATE_DIR); unset ⇒ in-memory. --thread-id NAME: the run identity
        # (also seeds idempotency keys). --max-fix-attempts N: the gate fix-loop bound.
        args, state_dir = _take_value(args, "--state-dir", what="a directory argument")
        args, _tid = _take_value(args, "--thread-id", what="a value")
        thread_id = _tid if _tid is not None else "cli-run"
        args, max_fix_attempts = _take_int(args, "--max-fix-attempts", minimum=1)

        # --model-mode {cloud|hybrid|local}: where the model levels run (cloud =
        # today's routing; hybrid = T1 on --backend, T2/T3 on the local server;
        # local = every level local). --local-url / --local-model name the local
        # OpenAI-compatible server (Ollama by default) and the model it serves.
        # --time-cap MINUTES stops the run cleanly once it has run that long.
        args, model_mode = _take_value(args, "--model-mode", what="a value")
        if model_mode is not None and model_mode not in MODEL_MODES:
            raise _CliUsageError(
                f"unknown --model-mode {model_mode!r}; choose one of {', '.join(MODEL_MODES)}"
            )
        args, _local_url = _take_value(args, "--local-url", what="a URL")
        local_url = _local_url or env_local_url()
        args, _local_model = _take_value(args, "--local-model", what="a model id")
        local_model = _local_model or env_local_model()
        args, time_cap = _take_int(args, "--time-cap", minimum=1)
        # --fix-until-cap / --no-fix-until-cap override FOUNDRY_FIX_UNTIL_CAP and the
        # mode's default (on for local).
        fix_until_cap: bool | None = None
        for flag, value in (("--fix-until-cap", True), ("--no-fix-until-cap", False)):
            if flag in args:
                fix_until_cap = value
                args = [a for a in args if a != flag]
        local_check = "--local-check" in args
        args = [a for a in args if a != "--local-check"]

        # Parallelism is SYSTEM-DECIDED: the Tech-Lead decomposes the work and the
        # IC pool sizes itself to that plan (min(tasks, AUTO_SLOTS_CAP)). --max-slots
        # / FOUNDRY_MAX_SLOTS only OVERRIDE the safety ceiling (e.g. lower it on a
        # constrained box); neither is required. --cost-cap-microusd N: the global
        # spend envelope the pool sub-allocates (default FOUNDRY_COST_CAP_MICROUSD).
        args, _slots = _take_int(args, "--max-slots", minimum=1)
        max_slots = _slots if _slots is not None else env_max_slots()

        _cost_env = os.environ.get("FOUNDRY_COST_CAP_MICROUSD", "").strip()
        cost_cap_microusd: int | None = int(_cost_env) if _cost_env.isdigit() else None
        args, _cost = _take_int(args, "--cost-cap-microusd")
        if _cost is not None:
            cost_cap_microusd = _cost
        # --token-cap N / FOUNDRY_TOKEN_CAP: stop the run cleanly once it has used N
        # tokens (cache reads count at a tenth). Works on subscription backends too.
        _tok_env = os.environ.get("FOUNDRY_TOKEN_CAP", "").strip()
        token_cap: int | None = int(_tok_env) if _tok_env.isdigit() else None
        args, _tok = _take_int(args, "--token-cap", minimum=1)
        if _tok is not None:
            token_cap = _tok

        # --workers frontend=2,backend=3,data=1,test=2 (or FOUNDRY_WORKERS): tickets
        # building at once per specialty, under the --max-slots ceiling. Default 4
        # each. --ticket-tiers trivial=T3,standard=T2,complex=T1 (or
        # FOUNDRY_TICKET_TIERS): the model tier each ticket difficulty runs on.
        args, _workers = _take_value(args, "--workers", what="specialty=N pairs")
        args, _tiers = _take_value(args, "--ticket-tiers", what="difficulty=TIER pairs")
        try:
            workers = parse_workers(
                _workers if _workers is not None else os.environ.get("FOUNDRY_WORKERS", "")
            )
            ticket_tiers = parse_ticket_tiers(
                _tiers if _tiers is not None else os.environ.get("FOUNDRY_TICKET_TIERS", "")
            )
        except ValueError as exc:
            raise _CliUsageError(str(exc)) from None

        # --profile NAME: the verification stack (pinned build/test/smoke commands).
        # OMITTED ⇒ "auto" (infer the stack from the request/spec keywords). A NAME
        # pins it (run() rejects an unknown name fail-closed). "none" keeps the bare
        # python defaults. Choices: python | python-app | node-web | pygame |
        # phaser-web | love2d | go | rust | dotnet | java-maven | crypto | iac-terraform.
        args, _profile = _take_value(args, "--profile", what="a stack name")
        profile = _profile if _profile is not None else "auto"

        # --scope {auto|demo|standard}: how much system to build. OMITTED ⇒ "auto"
        # (the planner classifies the request). A pinned tier is kept as-is.
        args, _scope = _take_value(args, "--scope", what="a value")
        scope = _scope if _scope is not None else "auto"
        if scope not in SCOPES:
            raise _CliUsageError(f"unknown --scope {scope!r}; choose one of {', '.join(SCOPES)}")

        # --adapter {stub|raw-react|claude-code}: the coding substrate that writes
        # the code. OMITTED ⇒ DERIVED from --backend in run() (mock→stub,
        # claude-cli→claude-code agentic, other real backends→raw-react) so a real
        # backend writes real code without a second flag.
        args, _adapter = _take_value(args, "--adapter", what="a value")
        if _adapter is not None and _adapter not in ADAPTERS:
            raise _CliUsageError(
                f"unknown --adapter {_adapter!r}; choose one of {', '.join(ADAPTERS)}"
            )
        adapter = _adapter  # None ⇒ run() derives it from --backend

        # --spec FILE|DIR (task [8]): may REPEAT to ingest several spec files/dirs.
        spec_paths: list[str] = []
        while "--spec" in args:
            args, _sp = _take_value(args, "--spec", what="a file or directory argument")
            if _sp is not None:
                spec_paths.append(_sp)
    except _CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if local_check:
        status = check_local_server(local_url, model=local_model)
        print(json.dumps(status.as_dict(), indent=2))
        return 0 if status.reachable and status.model_served is not False else 1
    # Resolve --spec paths to text FIRST: a spec can STAND IN for the one-line
    # request (run from just `--spec FILE`), so we resolve it before deciding whether
    # a positional request is required. A missing/empty path is a clean usage error
    # (exit 2); a deep ValueError from the run itself is not masked. The resolved text
    # is handed to run() (which seeds it onto `spec`).
    try:
        spec_text = _load_spec(spec_paths) if spec_paths else None
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The one-line request is OPTIONAL when a --spec is given: with no request we
    # derive one from the spec; with NEITHER a request nor a spec, print usage.
    try:
        request = _request_text(args)
    except _CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not request:
        if spec_text:
            request = _request_from_spec(spec_text)
        else:
            print(
                "usage: python -m foundry.cli "
                "[--mode assistant] "
                "[--backend {mock|anthropic|openai|claude-cli|codex}] "
                "[--adapter {stub|raw-react|claude-code}] "
                "[--scope {auto|demo|standard}] "
                "[--out DIR] [--base DIR] [--spec FILE|DIR ...] "
                "[--state-dir DIR] [--thread-id NAME] [--max-fix-attempts N] "
                "[--max-slots N] [--workers frontend=2,backend=2,data=2,test=2] "
                "[--ticket-tiers trivial=T3,standard=T2,complex=T1] "
                "[--model-mode {cloud|hybrid|local}] [--local-url URL] "
                "[--local-model ID] [--time-cap MINUTES] [--token-cap N] [--local-check] "
                "[--fix-until-cap|--no-fix-until-cap] "
                '["<one-line request>"]   (optional when --spec is given)\n'
                f"  mode: assistant — {_TRUST_POSTURE[MODE_ASSISTANT]}\n"
                "  backend: mock (default — keyless, deterministic) | anthropic | openai | "
                "claude-cli | codex (real model egress)\n"
                "  adapter (coding substrate): omitted => DERIVED from --backend "
                "(mock=>stub no-op, claude-cli=>claude-code AGENTIC, other real "
                "backends=>raw-react); override with stub | raw-react | claude-code\n"
                "  scope: auto (default — the planner classifies the request) | "
                "demo (small: capped criteria, 1 component, no interface contracts) | "
                "standard. Caps what the agents PROPOSE; the gate set is identical",
                file=sys.stderr,
            )
            return 2

    try:
        result = run(
            request,
            mode=mode,
            spec=spec_text,
            backend=backend,
            adapter=adapter,
            thread_id=thread_id,
            state_dir=state_dir,
            max_fix_attempts=max_fix_attempts,
            output_dir=output_dir,
            max_slots=max_slots,
            cost_cap_microusd=cost_cap_microusd,
            workers=workers,
            ticket_tiers=ticket_tiers,
            token_cap=token_cap,
            profile=(None if profile == "none" else profile),
            scope=scope,
            base_dir=base_dir,
            model_mode=model_mode,
            local_url=local_url,
            local_model=local_model,
            time_cap_minutes=time_cap,
            fix_until_cap=fix_until_cap,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RunCapExceeded as exc:
        flag = "--time-cap" if isinstance(exc, TimeCapExceeded) else "--token-cap"
        hint = (
            "Progress is saved: rerun the same command (same --thread-id and "
            f"--state-dir) with a higher {flag} to continue."
            if state_dir
            else "Add --state-dir DIR to make a capped run resumable."
        )
        print(f"stopped: {exc}. {hint}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        return 3
    print(result.summary())
    return 0 if result.passed else 1
