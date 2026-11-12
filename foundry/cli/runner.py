"""The smoke run: :func:`run` assembles the dev wiring, compiles the factory
graph, drives it, and writes the output."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from foundry import progress, telemetry
from foundry.agents import AgentRuntime
from foundry.agents.planner import TICKET_KIND
from foundry.cli import defaults, doubles, invoke, output, wiring
from foundry.cli import result as cli_result
from foundry.cli import spec as cli_spec
from foundry.cli.defaults import (
    _MOCK_BACKEND,
    AUTO_SLOTS_CAP,
    BACKENDS,
    MODES,
    SCOPES,
)
from foundry.cli.doubles import _PASSING_DIFF
from foundry.cli.invoke import RunCrashed
from foundry.cli.result import RunResult
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.persistence import DurableBackends, durable_backends, state_dir_from_env
from foundry.gates import TwoZoneGateRunner
from foundry.gates.reuse import VerdictCache
from foundry.graph.factory_graph import build_factory_graph
from foundry.graph.gate_wiring import FixPolicy
from foundry.graph.supervisor import MODE_ASSISTANT, resume_boundary
from foundry.manifest import write_manifest
from foundry.modes import (
    DEFAULT_FIX_ATTEMPTS,
    DEFAULT_FIX_UNTIL_CAP,
    Level,
    local_tiers,
    resolve_levels,
)
from foundry.proxy.token_meter import TokenMeter
from foundry.schemas.implementation import TaskEvent
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import max_epoch

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.state.factory_state import FactoryState


_SCANNER_REV = "scanner-rev-cli-1"


_LINEAGE_REV = "lineage-rev-cli-1"


_DETAIL_CAP = 200


def _detailed_artifacts(arts: Sequence[Any], cap: int = _DETAIL_CAP) -> list[Any]:
    """The artifacts a node's telemetry details, in order: every ticket (each must
    reach the Workshop's Tickets tab) plus the first ``cap`` of the bulk kinds."""
    out: list[Any] = []
    bulk = 0
    for a in arts:
        if getattr(a, "kind", None) == TICKET_KIND:
            out.append(a)
        elif bulk < cap:
            bulk += 1
            out.append(a)
    return out


# ---- the smoke run ----------------------------------------------------------


def _change_base(base_dir: str | None, request: str, thread_id: str) -> tuple[str | None, str]:
    """(absolute base tree, run id) for a change build; (None, thread_id) otherwise."""
    if base_dir is None:
        return None, thread_id
    from foundry.base_tree import change_thread_id, read_base

    root = Path(base_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"base {base_dir!r} is not a directory")
    if thread_id == "cli-run":
        thread_id = change_thread_id(root, request)
    if thread_id == read_base(root).thread_id:
        raise ValueError(
            f"thread {thread_id!r} is the base build's own run; a change needs a new one"
        )
    return str(root), thread_id


def _change_seed(base_root: str, *, has_stack: bool) -> FactoryState:
    """The base tree for the planner and gates, plus the base's recorded stack
    (a change keeps it) unless the operator pinned one."""
    from foundry.base_tree import read_base

    seed: FactoryState = {"base_dir": base_root}
    recorded = read_base(base_root).stack
    if not has_stack and recorded.get("language"):
        from foundry.schemas.implementation import StackDecision

        seed["stack"] = StackDecision.model_validate(recorded)
    return seed


def fix_until_cap_for(
    explicit: bool | None,
    *,
    model_mode: str | None,
    levels: Mapping[str, Any] | None,
    level_table: Mapping[str, Level] | None,
) -> bool:
    """Whether the run fixes until its time cap: ``explicit`` (the flag) wins, then
    ``FOUNDRY_FIX_UNTIL_CAP=0/1``, then the mode's default — for custom levels, on
    iff every level is local."""
    if explicit is not None:
        return explicit
    env = os.environ.get("FOUNDRY_FIX_UNTIL_CAP", "").strip()
    if env:
        if env not in ("0", "1"):
            raise ValueError(f"FOUNDRY_FIX_UNTIL_CAP must be 0 or 1, not {env!r}")
        return env == "1"
    if levels is not None and level_table is not None:
        return local_tiers(level_table) == frozenset(level_table)
    return DEFAULT_FIX_UNTIL_CAP[model_mode or "cloud"]


def run(
    request: str,
    *,
    mode: str = MODE_ASSISTANT,
    spec: str | list[str] | None = None,
    backend: str = _MOCK_BACKEND,
    thread_id: str = "cli-run",
    state_dir: str | None = None,
    resolve: ArtifactResolver | None = None,
    resolve_blob: BlobResolver | None = None,
    adapter: str | Any | None = None,
    diff_body: str | None = None,
    max_fix_attempts: int | None = None,
    output_dir: str | None = None,
    progress_cb: Callable[..., None] | None = None,
    activity_cb: Callable[[str], None] | None = None,
    task_cb: Callable[[TaskEvent], None] | None = None,
    workers: Mapping[str, int] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
    max_slots: int = AUTO_SLOTS_CAP,
    cost_cap_microusd: int | None = None,
    token_cap: int | None = None,
    profile: str | None = "auto",
    scope: str = "auto",
    base_dir: str | None = None,
    model_mode: str | None = None,
    levels: Mapping[str, Any] | None = None,
    local_url: str | None = None,
    local_model: str | None = None,
    time_cap_minutes: float | None = None,
    cancel: threading.Event | None = None,
    manifest_dir: str | None = None,
    fix_until_cap: bool | None = None,
    log_cb: Callable[[str], None] | None = None,
    local_thinking: str | None = None,
    self_check: str | None = None,
    native_tools: bool | None = None,
) -> RunResult:
    """Run ``request`` end-to-end through the compiled factory graph (smoke path).

    Assembles the full dev wiring with the test doubles (NO live API key, NO
    ``claude -p`` binary, NO network), compiles :func:`build_factory_graph`,
    invokes it with ``{"request": request}`` + a ``thread_id`` config, and
    returns a :class:`RunResult` reporting the reviewed artifact handle, the
    single ``open_pr`` :class:`ExternalAction`, and the attested gate statuses.

    Parameters
    ----------
    request:
        The one-line natural-language request (e.g. ``"build a URL shortener"``).
    mode:
        Accepted for compatibility; only ``"assistant"`` is valid.
    spec:
        OPTIONAL full design-spec document (task [8]). ADDITIVE to ``request``: a
        short request summarizes intent while the spec provides the detail. May be
        a path to a ``.md``/``.txt`` FILE, a path to a DIRECTORY of such files
        (read recursively, sorted), a list of paths, or literal spec text. The
        resolved text is seeded onto the ``spec`` state channel, where the
        planner GROUNDS the IntentBrief/PRD/design in it. It is plain input data
        — it INFORMS the criteria but never bypasses the acceptance-criteria
        derivation or any signed gate.
    thread_id:
        The run identity (LangGraph ``thread_id`` / ``Seq.run_id``); also seeds
        the deterministic idempotency keys (verdict nonce, PR key).
    resolve / resolve_blob:
        Materialization seams (GW1a/D-R1-02). Default to the proven synthetic
        SUP7 doubles (which fall through to the real ``rt.cas`` views) so the
        SPEC core path reaches a clean Spec/Build/Test PASS with the StubAdapter.
    diff_body:
        The unified-diff body the synthetic ``resolve_blob`` returns for the
        FeatureDiff. ``None`` (the default) selects the passing ``_PASSING_DIFF``. Pass an explicit
        error/failing body to exercise the halted-run path.
    max_fix_attempts:
        Convergence bound for the gate fix-loop (default 3 — 4 in the hybrid and 5
        in the local model mode: two fixer passes per
        failing boundary — each per-wave check and the final gate). On a gate FAIL/ERROR the
        run fixes FORWARD — back to the responsible layer WITH the specific failures —
        up to this many times before HALTing (escalate). The failed tree's digest stays
        permanently disqualified each round (anti-gaming); only a genuinely-passing NEW
        tree ships. Raise it for "keep working until it's green"; 0 disables retries.
    output_dir:
        When set AND the build PASSES (terminal gates pass), write a single
        **GitHub-ready repository** here: the accepted integrated source tree at
        the root + the ``BUILD-MANIFEST`` (json + md, with model/token usage).
        Surfaced on ``RunResult.output_root``. On a HALTED run a
        best-effort DRAFT (the latest attempt's tree + a ``DRAFT-STATUS.md`` naming
        what's failing) is written instead, surfaced on ``RunResult.draft_root`` — so
        a held build hands back a usable starting point, never empty hands.
    manifest_dir:
        Where to write the ``BUILD-MANIFEST`` when no tree is written to
        ``output_dir`` (the Workshop keeps code in the store but still reports it).
    task_cb / workers / ticket_tiers:
        Ticket path (the planner emitted tickets). ``task_cb`` receives each
        ticket's :class:`TaskEvent` (``{phase, ticket_id, title, specialty, tier}``,
        phase start/done/failed) as the worker pool runs it; ``workers`` caps
        concurrent tickets per specialty (default 4 each) under ``max_slots``;
        ``ticket_tiers`` maps difficulty -> model tier (default
        trivial=T3, standard=T2, complex=T1).
    scope:
        How much system to build. ``"auto"`` (the DEFAULT) lets the planner
        node classify the request; ``"demo"`` / ``"standard"`` PIN the tier so
        the planner echoes it instead of judging it. Scope caps only what the
        planner PROPOSES (criteria count, components, interface contracts,
        ownership units) — every deterministic gate a build must pass is
        identical at both tiers, so this can never release a weaker build. Pin it
        for a reproducible A/B against the judged tier.
    base_dir:
        A shipped build's tree to CHANGE (``--base``): ``request`` is the change.
        Every ticket sandbox and gate starts from that tree, the planner plans only
        the delta, every gate (the base's own scenarios included) runs on base +
        delta, and ``output_dir`` receives the full updated tree. The base's
        recorded stack is kept. The default ``thread_id`` becomes a per-change id;
        reusing the base build's own thread is refused.
    model_mode / levels / local_url / local_model:
        Where each model level runs (Phase 4b). ``model_mode`` is a preset —
        ``"cloud"`` (today's routing, the same as None), ``"hybrid"`` (T1 on
        ``backend``, T2/T3 local) or ``"local"`` (every tier local). ``levels`` is
        an explicit table ``{"T1": {"backend", "model"[, "base_url"]}, ...}`` that
        overrides the preset (omitted tiers run on ``backend``). A local level is an
        OpenAI-compatible server at ``local_url`` (default Ollama's
        ``http://localhost:11434/v1``) serving ``local_model``. Local calls are
        unmetered and serialized (one local call/session at a time); tickets on a
        local level run the proxy-agent loop, tickets on a cloud level the run's
        cloud adapter.
    time_cap_minutes:
        Stop cleanly (resumable, like ``token_cap``) once the run has used this
        much wall-clock time, across restarts.
    cancel:
        Setting this event stops the run cleanly with ``BuildStopped`` before the
        next model call, coding session or gate run (the token meter's seams). The
        checkpoint stays resumable, as after a token-cap stop.
    fix_until_cap:
        Run each build-side fix loop until a dead-end rule or the time cap stops
        it, instead of ``max_fix_attempts`` passes (see
        :class:`~foundry.graph.gate_wiring.FixPolicy`). ``None``: see
        :func:`fix_until_cap_for` (on for local runs).
    log_cb:
        Receives every backend progress line (:mod:`foundry.progress`) for the run,
        auto-resume waits included, whatever ``FOUNDRY_PROGRESS`` says.
    local_thinking / self_check / native_tools:
        The run's ``FOUNDRY_LOCAL_THINKING`` (off/on/agent), ``FOUNDRY_SELF_CHECK``
        and ``FOUNDRY_NATIVE_TOOLS``; ``None`` reads the env. Run-scoped, so
        concurrent runs in one process don't share them.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}")
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; choose one of {', '.join(SCOPES)}")
    if diff_body is None:
        diff_body = _PASSING_DIFF
    level_table = resolve_levels(
        model_mode=model_mode,
        levels=levels,
        backend=backend,
        cloud_models=wiring._tier_models(backend),
        cloud_backends=BACKENDS,
        local_url=local_url,
        local_model=local_model,
    )
    if max_fix_attempts is None:
        max_fix_attempts = DEFAULT_FIX_ATTEMPTS[model_mode or "cloud"]
    if time_cap_minutes is not None and time_cap_minutes <= 0:
        raise ValueError("time_cap_minutes must be > 0")
    fix_policy = FixPolicy(
        until_cap=fix_until_cap_for(
            fix_until_cap, model_mode=model_mode, levels=levels, level_table=level_table
        )
    )
    if fix_policy.until_cap and time_cap_minutes is None:
        print(
            f"note: fixing until the time cap without one: each fix loop stops after "
            f"{fix_policy.window_ceiling} passes (set --time-cap to fix for longer)",
            file=sys.stderr,
        )
    spec_text = cli_spec._load_spec(spec)
    base_root, thread_id = _change_base(base_dir, request, thread_id)
    # Single-host durability seam (durability/persistence.py): when
    # FOUNDRY_STATE_DIR is SET, the checkpointer,
    # the GlobalRecords trust ledgers and the CAS are all file-backed under that
    # dir so the run survives a process restart. UNSET (the DEFAULT) ⇒ in-memory
    # everywhere — byte-identical to the historical request-only run.
    # An explicit state_dir= (the --state-dir flag) OVERRIDES the env; otherwise
    # fall back to FOUNDRY_STATE_DIR. None either way ⇒ in-memory (the default).
    resolved_state_dir = state_dir if state_dir is not None else state_dir_from_env()
    durable: DurableBackends | None = (
        durable_backends(resolved_state_dir) if resolved_state_dir is not None else None
    )
    if durable is not None:
        # FilesystemArtifactStore is a documented structural drop-in for the
        # in-memory ArtifactStore (identical put_body/.../resolve/resolve_blob
        # seams, pinned by tests/test_fs_artifact_store.py); cast so the rest of
        # the assembly reads it as the same cas type with no call-site change.
        store: ArtifactStore = cast("ArtifactStore", durable.cas)
        records = durable.global_records
        ckpt = durable.checkpointer
    else:
        store = ArtifactStore()
        records = GlobalRecords()
        ckpt = FoundryCheckpointer()
    if level_table is None:
        proxy = wiring._build_proxy(backend, local_thinking=local_thinking)
        # --adapter omitted (None) ⇒ DERIVE the substrate from the backend so a real
        # brain writes real code (mock→stub, any real backend→raw-react). An explicit
        # adapter — a name OR an injected WorkerAdapter instance (tests) — wins.
        if adapter is None:
            adapter = defaults._default_adapter_for(backend)
        coding_adapter = wiring._resolve_coding_adapter(
            adapter,
            proxy=proxy,
            thread_id=thread_id,
            activity_cb=activity_cb,
            tier_models=wiring._tier_models(backend),
            native_tools=native_tools,
        )
    else:
        proxy = wiring._build_level_proxy(level_table, local_thinking=local_thinking)
        coding_adapter = wiring._level_adapter(
            adapter,
            backend=backend,
            levels=level_table,
            proxy=proxy,
            thread_id=thread_id,
            activity_cb=activity_cb,
            native_tools=native_tools,
        )
        if local_tiers(level_table) == frozenset(level_table):
            max_slots = 1  # one local model: more ticket slots would only queue
    rt = AgentRuntime(proxy=proxy, adapter=coding_adapter, cas=store)
    run_dir: Path | None = None
    if resolved_state_dir is not None:
        run_key = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
        run_dir = Path(resolved_state_dir) / "calls" / run_key
        proxy.call_cache = run_dir
    meter: TokenMeter | None = None
    if token_cap is not None or time_cap_minutes is not None or cancel is not None:
        meter = TokenMeter(
            token_cap,
            run_dir / "tokens.json" if run_dir else None,
            cancel=cancel,
            time_cap_s=time_cap_minutes * 60 if time_cap_minutes else None,
        )
        proxy.token_meter = meter
        if hasattr(rt.adapter, "token_meter"):  # the claude-code substrate bypasses the proxy
            setattr(rt.adapter, "token_meter", meter)  # noqa: B010

    # Seal the zone-A observation channel (fail-closed forged-results hardening):
    # the gate-wiring builds each gate with `runner.seal_key`, so the observation a
    # gate produces is sealed under the same per-run key the runner verifies.
    # Gate units re-run on identical inputs reuse the original attested verdicts; under
    # the run's state dir so a resumed process reuses what the interrupted one judged.
    runner = TwoZoneGateRunner(
        wiring._zone_b_signer(),
        records,
        _SCANNER_REV,
        _LINEAGE_REV,
        seal_key=os.urandom(32),
        verdicts=VerdictCache(run_dir / "verdicts" if run_dir is not None else None),
    )

    # -- materialization seam (Phase-1 2b) ------------------------------------
    # The `mock` smoke backend uses the synthetic SUP7 doubles: with the
    # StubAdapter no faithful test body is ever authored and the planner
    # mints epoch-keyed handles, so the doubles synthesize faithful bodies
    # that guarantee the deterministic Spec/Build/Test PASS. A REAL backend means
    # the agents author real designs / criteria / ownership AND (with a real
    # coding adapter) a real FeatureDiff is written into the CAS, so we resolve
    # straight off the real store — the bytes the gates run are EXACTLY what was
    # authored, never a canned override. (A real backend with the `stub` adapter
    # authors no diff; the reviewer then fail-closes honestly — a real build needs
    # a real coding adapter.)
    real_artifacts = backend != _MOCK_BACKEND
    _resolve, _resolve_blob = doubles._materialization_seam(
        real_artifacts, store=store, diff_body=diff_body
    )
    resolve = resolve or _resolve
    resolve_blob = resolve_blob or _resolve_blob

    # -- judgment-binding anchors (Phase-1 2c) --------------------------------
    # `subject_artifact_uri` / `state_sha` are pre-build RUN-INPUT LABELS the
    # reviewer's advisory verdict binds to. For the `mock`
    # smoke they are PINNED to the synthetic subject/label the synthetic resolver
    # produces (byte-identical to before). For a REAL backend we DON'T pin them:
    # the reviewer then binds the REAL newest FeatureDiff uri (state fallback) +
    # the REAL integrated tree_sha (read off the attested gate verdicts), so the
    # verdict honestly cites the artifact that was actually built. The gates and
    # the stable-FAIL latch use the real verdict-carried `run_against`, never
    # this label, so unpinning is safe.
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "verdict_nonce": f"N-{thread_id}",
        **doubles._judgment_anchors(real_artifacts),
    }
    config: RunnableConfig = {
        "configurable": configurable,
        "metadata": {"langgraph_step": 1},
        "recursion_limit": 200,
    }

    # Install the dev gate-write verifier so the REAL merge_gates admits the
    # runner-attested verdicts (D4); reset its token in finally so it never leaks.
    token = set_gate_write_verifier(wiring._gate_verifier(records))
    with (
        progress.sink(log_cb) if log_cb is not None else contextlib.nullcontext(),
        telemetry.counting() as fire_counts,
        tempfile.TemporaryDirectory(prefix="foundry-cli-src-") as source_dir,
    ):
        try:
            if base_root is not None:
                from foundry.base_tree import copy_base_tree

                copy_base_tree(base_root, source_dir)
            # Verification profile (ROADMAP Phase 3): stack-appropriate pinned
            # gate commands. "auto" (default) infers from the request + spec;
            # None keeps the historical python defaults; a profile NAME pins
            # that profile explicitly.
            from foundry.gates.profiles import PROFILES, profile_for_request

            if profile == "auto":
                run_profile = profile_for_request(f"{request}\n{spec_text or ''}")
            elif profile is None:
                run_profile = None
            else:
                if profile not in PROFILES:
                    raise ValueError(
                        f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}"
                    )
                run_profile = PROFILES[profile]
            app = build_factory_graph(
                rt,
                runner=runner,
                resolve=resolve,
                resolve_blob=resolve_blob,
                source_dir=source_dir,
                checkpointer=ckpt.saver,
                max_fix_attempts=max_fix_attempts,
                profile=run_profile,
                max_slots=max_slots,
                cost_cap_microusd=cost_cap_microusd,
                workers=workers,
                ticket_tiers=ticket_tiers,
                on_task=task_cb,
                fix_policy=fix_policy,
                self_check_mode=self_check,
            )
            # The epoch counter is in-memory: a process continuing this thread's
            # checkpoint must mint Seqs above the run's records, or a re-run gate's
            # verdict would rank below the stale one it supersedes.
            prior = invoke._prior_values(app, config)
            if prior:
                rt.epochs.raise_floor(thread_id, max_epoch(prior, thread_id))
            # `spec` is additive and seeded ONLY when provided (request-only runs
            # keep the historical {"request": ...} seed exactly). The product
            # nodes read it off the `spec` channel to ground the IntentBrief/PRD.
            seed: FactoryState = {"request": request}
            if spec_text:
                seed["spec"] = spec_text
            # A named profile pins the stack the planner would otherwise choose.
            if profile is not None and profile != "auto":
                from foundry.gates.profiles import pinned_stack

                seed["stack"] = pinned_stack(profile)
            if base_root is not None:
                seed.update(_change_seed(base_root, has_stack="stack" in seed))
            # An operator-pinned scope seeds the channel the planner would
            # otherwise write; the planner sees a decision already present and
            # keeps it. `auto` (the default) seeds nothing,
            # so the historical seed is unchanged.
            if scope != "auto":
                from foundry.schemas.governance import ScopeDecision, ScopeTier

                seed["scope_tier"] = ScopeDecision(
                    tier=ScopeTier(scope),
                    rationale=f"Pinned by the operator (--scope {scope}).",
                    seq=rt.seq(config, "cli", "scope_tier"),
                )
            # LIVE artifact CONTENT for per-node telemetry: resolve each finishing
            # node's artifacts through the run's store and attach a short content
            # one-liner per handle (the PRD's title, an acceptance criterion's text,
            # a design's components …). This is what lets a live event log show WHAT
            # each agent produced, not just the artifact kind. Advisory + best-effort:
            # any resolve/format failure is swallowed (the handle just carries no
            # detail). Only wired when a progress_cb is attached.
            drive_pc = progress_cb
            if progress_cb is not None:
                from foundry.schemas.checkpoint import _artifact_detail, _org_section

                def _feature_diff_stat(body: object) -> str | None:
                    # An engineer's "what I built": the FeatureDiff handle has no content
                    # fields, so resolve its diff BLOB and summarise it — changed files +
                    # size. claude-code / proxy adapters store a JSON full-file snapshot
                    # ({"files": {path: content}, "deleted": [...]}); raw-react stores a
                    # unified diff. Handle both; return None (→ bare "feature diff") on
                    # anything unrecognised.
                    uri = getattr(body, "diff_uri", None)
                    if not uri:
                        return None
                    raw = resolve_blob(uri)
                    text = (
                        raw.decode("utf-8", "replace")
                        if isinstance(raw, (bytes, bytearray))
                        else str(raw)
                    )
                    snap: Any = None
                    try:
                        snap = json.loads(text)
                    except Exception:
                        snap = None
                    if isinstance(snap, dict) and isinstance(snap.get("files"), dict):
                        files = list(snap["files"])
                        deleted = snap.get("deleted") or []
                        lines = sum(
                            c.count("\n") + 1 for c in snap["files"].values() if isinstance(c, str)
                        )
                        names = ", ".join(os.path.basename(f) for f in files[:6])
                        if len(files) > 6:
                            names += " …"
                        parts = [f"{len(files)} file(s)"]
                        if lines:
                            parts.append(f"~{lines} lines")
                        if deleted:
                            parts.append(f"{len(deleted)} deleted")
                        stat = ", ".join(parts)
                        return f"{stat} — {names}" if names else stat
                    files_u: list[str] = []
                    adds = dels = 0
                    for ln in text.splitlines():
                        if ln.startswith("+++ "):
                            pth = ln[4:].strip()
                            if pth and pth != "/dev/null":
                                files_u.append(pth[2:] if pth[:2] in ("a/", "b/") else pth)
                        elif ln.startswith("+") and not ln.startswith("+++"):
                            adds += 1
                        elif ln.startswith("-") and not ln.startswith("---"):
                            dels += 1
                    if not (files_u or adds or dels):
                        return None
                    names = ", ".join(os.path.basename(f) for f in files_u[:6])
                    if len(files_u) > 6:
                        names += " …"
                    stat = f"{len(files_u)} file(s) +{adds}/-{dels}"
                    return f"{stat} — {names}" if names else stat

                def drive_pc(
                    node: str,
                    scope: str,
                    *,
                    phase: str = "finish",
                    result: object = None,
                    _pc: Any = progress_cb,
                    _rv: Any = resolve,
                ) -> None:
                    if phase == "finish" and isinstance(result, dict) and result.get("artifacts"):
                        arts = list(result["artifacts"])
                        details: list[dict[str, str]] = []
                        for a in _detailed_artifacts(arts):
                            kind = str(getattr(a, "kind", ""))
                            try:
                                body = _rv(a)
                                d = (
                                    _feature_diff_stat(body)
                                    if kind == "feature_diff"
                                    else _artifact_detail(kind, body, max_len=600)
                                )
                            except Exception:
                                d = None
                            details.append(
                                {
                                    "kind": kind,
                                    "detail": str(d or ""),
                                    "uri": str(getattr(a, "uri", "")),
                                    "produced_by": str(getattr(a, "produced_by", "")),
                                }
                            )
                        if details:
                            result = {**result, "_art_details": details}
                        org = _org_section({"artifacts": arts}, _rv)
                        if org is not None:
                            result = {**result, "_org": org}
                    _pc(node, scope, phase=phase, result=result)

            start = invoke._resume_seed(app, config, seed)
            if meter is not None and start is seed and not invoke._has_pending_run(app, config):
                meter.reset()  # a fresh run, not a resumed one: its spend starts at zero
            try:
                final = invoke._invoke_with_autoresume(
                    app,
                    start,
                    config,
                    progress_cb=drive_pc,
                    resumable=(durable is not None),
                    cancel=cancel,
                    meter=meter,
                )
            except Exception as exc:
                # A capped, stopped, unavailable or crashed run still hands back its
                # best tree.
                if output_dir is not None:
                    output._draft_on_stop(
                        app,
                        config,
                        exc,
                        request=request,
                        thread_id=thread_id,
                        output_dir=output_dir,
                        resolve=resolve,
                        resolve_blob=resolve_blob,
                    )
                if isinstance(exc, ValueError):
                    # main() reports a ValueError as a usage error (exit 2, no
                    # traceback); one raised inside the graph is a crash and must
                    # show where.
                    crashed = RunCrashed(f"the run crashed: {exc!r}")
                    for note in getattr(exc, "__notes__", ()):
                        crashed.add_note(note)
                    raise crashed from exc
                raise
        finally:
            if run_dir is not None:
                # This process's counts only, written even when the run stops or
                # raises: a caller resuming a run (scripts/bench.py) sums segments.
                with contextlib.suppress(OSError):
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / telemetry.COUNTS_FILE).write_text(json.dumps(fire_counts.snapshot()))
            reset_gate_write_verifier(token)
            rt.python_envs.close()  # the run's gate venvs end with the run
            rt.toolchain_cache.close()
            # Release the SQLite handles: durable mode closes the checkpointer +
            # the global-records connection together; otherwise just the ckpt.
            if durable is not None:
                durable.close()
            else:
                ckpt.close()

    final_state = final
    result = cli_result._result(
        request,
        thread_id,
        final_state,
        mode=mode,
        backend=backend,
    )
    result = replace(result, hint_counts=fire_counts.snapshot())
    if level_table is not None:
        result = replace(
            result,
            model_mode="custom" if levels else model_mode,
            levels={t: lvl.as_dict() for t, lvl in level_table.items()},
        )
    if final_state.get("phase") == "halted":
        result = replace(
            result, resume_from=resume_boundary(final_state, resolve), resumable=durable is not None
        )
    shipped_sha = ""
    if output_dir is not None and result.passed:
        # On a PASSING build, materialize the FINAL accepted integrated tree
        # (reusing the SAME materialize_workdir + resolve/resolve_blob seams the
        # terminal gates ran against) and write the real source files to disk.
        # A HALTED run writes a best-effort DRAFT instead (see the elif below).
        output_root, shipped_sha = output._write_shipped_tree(
            final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob
        )
        result = replace(result, output_root=output_root)
    elif output_dir is not None and not result.passed:
        # HALTED with an output dir: still hand back a usable DRAFT (the best attempt
        # + a DRAFT-STATUS.md saying what's failing), so the run is never empty-handed.
        draft_root, status = output._write_draft_on_halt(
            final_state,
            output_dir,
            gate_findings=result.gate_findings,
            resolve=resolve,
            resolve_blob=resolve_blob,
        )
        result = replace(result, draft_root=draft_root, draft_status=status if draft_root else None)
    elif not result.passed and output._has_draft_tree(final_state):
        status = output._draft_status(
            final_state,
            gate_findings=result.gate_findings,
            best=output._draft_best(final_state, resolve),
        )
        result = replace(result, draft_status=status)
    manifest_root = output_dir if (result.output_root or result.draft_root) else manifest_dir
    if manifest_root:
        # The manifest is a report on a finished build: failing to write it must
        # never turn a gated result into an exception.
        try:
            write_manifest(
                manifest_root,
                request=request,
                thread_id=thread_id,
                state=final_state,
                rt=rt,
                passed=result.passed,
                tree_sha=shipped_sha,
            )
        except Exception as exc:
            print(f"warning: BUILD-MANIFEST not written: {exc}", file=sys.stderr)
    return result
