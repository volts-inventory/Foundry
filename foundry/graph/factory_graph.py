r"""Factory graph — compile the full Phase-1 spine + the Gated-mode emit (T-M1-14 /
T-M2-10).

(architecture.md §1/§5/§7, governance §1/§5/§6, implementation-plan §7)

This is the TOP-LEVEL graph: it mounts the supervisor (the phase-routing spine
across the role subgraphs + the deterministic gate boundaries; T-M1-13/T-M2-09)
as a single subgraph node, appends a MODE-SPECIFIC emit node, and compiles the
whole thing with the shared durable checkpointer so the run is resumable.

Topology::

    START
      -> factory     # the supervisor subgraph (mode-selected: assistant | gated)
      -> emit        # mode-selected emit of the PR-equivalent ExternalAction
      -> END

The ``emit`` node runs ONLY after the supervisor's phases complete. It is the
ONLY place a Phase-1/Phase-2 external EFFECT (a PR) is requested, and it does so
the way ``foundry/graph/skeleton.py`` does — by writing an :class:`ExternalAction`
to the ``actions`` channel with a DETERMINISTIC idempotency key derived via
:func:`content_digest` (NOT wall clock, L7). The outbox relay
(``foundry/durability``) is the SOLE component that ever touches the outside
world; nothing here makes a GitHub / network call. A kill-then-resume re-folds
the committed ``emit`` write and re-derives the SAME idempotency key, so
``union_actions`` (keyed by ``idempotency_key``) coalesces it — no duplicate PR
(Phase-0 exit criterion).

Two emit modes (``mode=``, threaded through to ``build_supervisor_graph``):

* ASSISTANT (default, Phase-1, FG1-3) — guard on the terminal gate PASS
  (Spec+Build+Test) and open a bare ``open_pr`` against the integrated
  FeatureDiff. No evidence pack. Back-compat: byte-for-byte the Phase-1 emit.

* GATED (Phase-2, T-M2-10) — a RELEASE that travels with its WORM evidence pack
  (governance §6). The gated emit guards on (1) the terminal gate PASS AND the
  tier-required assurance gates PASS, AND (2) ``release_allowed(...)`` — a
  recorded, approved Release sign-off bound to THIS exact decision point. Only
  then does it build a tamper-evident :class:`EvidencePack` (hash-chained,
  content-addressed, over the attested GateResults + released artifact handles +
  the recorded SignOffRecords) and emit an ``open_pr`` whose payload REFERENCES
  the pack's ``pack_uri`` (the release travels with its evidence). Fail-closed: a
  halted / failed / unsigned-release run emits NOTHING — no PR, no pack release.

PASS GUARD (governance §1/§5/§6): the assistant PR is emitted ONLY on a clean
Spec+Build+Test PASS; the gated PR additionally requires the tier's assurance
gates PASS and an admitted Release sign-off. A run that halted (a stable-FAIL
latch, an unresolved ERROR, the convergence bound spent, a blocked release) emits
NOTHING — we never open a PR for a non-released run. Both guards read only the
trust-filtered ``gates`` channel (verdicts are runner-attested; an unsigned write
never reaches here, L5), so they cannot be tricked by an unattested status-flipped
copy.

The ``add_node`` mypy seam (LEARNINGS SG2/IM5): a closure typed as the ``NodeFn``
``Callable`` alias trips ``add_node``'s overload resolution, so each emit node is
a thin concrete ``def`` (no ``cast``, no private import). The supervisor subgraph
is added as a compiled graph, which ``add_node`` accepts directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation.senior_engineer import DESIGN_KIND
from foundry.canonical import content_digest
from foundry.durability.evidence_pack import build_evidence_pack
from foundry.governance.release_anchors import (
    evidence_hash,
    newest_artifact_uri,
    newest_release_sign_off,
    sign_off_nonce,
    state_sha,
)
from foundry.governance.sign_off import release_allowed
from foundry.graph.assurance_wiring import assurance_gate_set
from foundry.graph.gate_wiring import FEATURE_DIFF_KIND
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_GATED,
    build_supervisor_graph,
)
from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.schemas.gates import GateStatus
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact
    from foundry.schemas.gates import GateResult
    from foundry.schemas.governance import SignOffRecord

__all__ = [
    "TERMINAL_PASS_GATES",
    "build_factory_graph",
    "gated_release_admitted",
    "terminal_gates_pass",
]

# Supervisor subgraph node id + the emit node id.
_FACTORY = "factory"
_EMIT = "emit"

# The PR is opened only when EVERY terminal gate has a PASS verdict (governance
# §1/§5): the Spec gate (product/architecture boundary) AND the Build + Test
# gates (implementation/quality boundary). The reviewer's ``review`` verdict is
# advisory here — the deterministic gates own the release bit (LEARNINGS L5).
TERMINAL_PASS_GATES: frozenset[str] = frozenset({"spec", "build", "test"})

# The release-evidence "released artifact" the pack carries + the PR opens against
# — the integrated FeatureDiff handle (a release travels with its diff).
_RELEASED_ARTIFACT_KIND = FEATURE_DIFF_KIND


def _latest_by_name(state: FactoryState) -> dict[str, Any]:
    """Newest-by-``Seq`` verdict per gate name on the (trust-filtered) channel.

    ``gates`` is admitted only for runner-attested records (``merge_gates``); pick
    the latest by ``Seq`` per name so a re-verification run's verdict (bumped
    epoch) wins over the stale record it supersedes.

    NB: this collapses by ``name`` ALONE and takes the global newest-by-``Seq``,
    whereas ``merge_gates`` keys by ``(name, run_against)``. That is deliberate
    release semantics: across a fix-loop the channel may hold same-name verdicts
    for different ``run_against`` trees; the newest integrated tree's verdict is
    the one that gates the release, so cross-``run_against`` newest-wins is correct.
    """
    latest: dict[str, Any] = {}
    for g in state.get("gates") or ():
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def _gates_all_pass(latest: dict[str, Any], names: frozenset[str] | tuple[str, ...]) -> bool:
    """True iff EVERY named gate has a newest-by-``Seq`` PASS verdict (fail-closed:
    a missing verdict or any non-PASS status is False)."""
    for name in names:
        verdict = latest.get(name)
        if verdict is None or verdict.status is not GateStatus.PASS:
            return False
    return True


def terminal_gates_pass(state: FactoryState) -> bool:
    """True iff EVERY terminal gate has a newest-by-``Seq`` PASS verdict.

    Fail-closed: a missing terminal verdict, or any non-PASS status, means the
    run did NOT complete a clean release boundary — no PR. Reads only the
    trust-filtered ``gates`` channel (verdicts are runner-attested; an unsigned
    write never reaches here, L5), so the guard cannot be tricked by an
    unattested status-flipped copy.
    """
    return _gates_all_pass(_latest_by_name(state), TERMINAL_PASS_GATES)


def _required_gate_names(state: FactoryState) -> frozenset[str]:
    """The full set of gate names a GATED release requires PASS — the terminal
    gates (Spec+Build+Test) PLUS the tier-scaled assurance family.

    The assurance depth is read off the ``risk_tier`` single-writer channel via
    :func:`assurance_gate_set` (gov §1e — the tier scales DEPTH, never integrity:
    the Security+Audit FLOOR is always required; STANDARD adds supply_chain; HIGH
    adds e2e). So a HIGH-tier release that skipped supply_chain/e2e can never be
    admitted by this guard."""
    assurance = assurance_gate_set(state.get("risk_tier"))
    return TERMINAL_PASS_GATES | frozenset(assurance)


def gated_release_admitted(state: FactoryState, config: RunnableConfig) -> bool:
    """True iff a GATED release may emit (fail-closed; governance §1/§5/§6).

    Two independent guards, BOTH required:

    * EVERY required gate PASS — the terminal gates (Spec+Build+Test) AND the
      tier-scaled assurance family (:func:`_required_gate_names`), newest-by-Seq
      on the trust-filtered ``gates`` channel (runner-attested only, L5);
    * ``release_allowed(...)`` — a recorded, approved Release sign-off that binds
      THIS exact decision point (subject / state_sha / evidence hash / nonce),
      re-derived against the SAME anchors the supervisor's release boundary bound
      (no drift: both import the PUBLIC anchor helpers from
      ``foundry.governance.release_anchors``). No record / not approved
      / mismatched anchors ⇒ not released.

    A halted / failed / unsigned-release run is False here → the gated emit opens
    NOTHING.
    """
    if not _gates_all_pass(_latest_by_name(state), _required_gate_names(state)):
        return False
    subject_uri = newest_artifact_uri(state, DESIGN_KIND)
    sha = state_sha(config)
    nonce = sign_off_nonce(config, "release_sign_off", subject_uri, sha)
    ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
    record = newest_release_sign_off(state)
    return release_allowed(
        record,
        subject_artifact_uri=subject_uri,
        state_sha=sha,
        presented_evidence_hash=ev_hash,
        server_nonce=nonce,
    )


def _pr_subject(state: FactoryState) -> Artifact | None:
    """The integrated FeatureDiff handle the PR opens against (newest-by-``Seq``).

    ``artifacts`` folds by ``merge_by_uri`` (insertion order, not ``Seq``), so we
    pick the ``feature_diff`` handle with the max ``Seq`` deterministically
    (mirrors ``gate_wiring`` / ``reviewer_critic`` newest-by-Seq selection) —
    NOT ``artifacts[-1]``.
    """
    subject: Artifact | None = None
    for a in state.get("artifacts") or ():
        if a.kind != FEATURE_DIFF_KIND:
            continue
        if subject is None or a.seq > subject.seq:
            subject = a
    return subject


def _attested_terminal_results(state: FactoryState) -> tuple[GateResult, ...]:
    """The newest-by-``Seq`` attested GateResult for each required release gate,
    ordered by gate name (a stable, deterministic order so the evidence pack's
    chain head is reproducible across resume — L7).

    These are the verdicts the release is admitted on — exactly the ones the
    evidence pack must chain over (governance §4/§6: the WORM-anchored attested
    verdicts)."""
    latest = _latest_by_name(state)
    required = _required_gate_names(state)
    return tuple(
        latest[name] for name in sorted(required) if name in latest
    )


def _attested_run_against(gate_results: tuple[GateResult, ...]) -> str | None:
    """The actual materialized ``tree_sha`` the gates ran against — read off one of
    the attested terminal/assurance GateResults the pack folds over (every
    GateResult carries the TRUE ``run_against`` of the integrated tree it was
    evaluated on).

    Binding the pack's ``run_against`` to an attested verdict's ``run_against``
    (rather than a config-supplied ``state_sha`` LABEL) makes the pack's stated
    tree HONEST — it is the exact tree the gates were actually run over (M2-R1-02).
    The required release gates all share ONE integrated ``tree_sha`` (GW2), so any
    folded-over verdict gives the same value; ``None`` only when no verdict exists
    (admission already fail-closed before this is reached)."""
    for g in gate_results:
        if g.run_against:
            return g.run_against
    return None


def _scanner_config_rev(gate_results: tuple[GateResult, ...]) -> str | None:
    """The pinned scanner-config revision the release was assured under — read off
    an attested verdict (the runner stamps ``scanner_config_rev`` on every
    GateResult; AS4), NOT the runner's private attribute. ``None`` when no attested
    verdict carries one (e.g. judgment-only) — the pack records it as absent."""
    for g in gate_results:
        if g.scanner_config_rev is not None:
            return g.scanner_config_rev
    return None


def _released_artifacts(state: FactoryState) -> tuple[Artifact, ...]:
    """The released artifact handles the evidence pack carries — the integrated
    FeatureDiff (newest-by-Seq). A pure function of state so the pack is stable on
    resume."""
    subject = _pr_subject(state)
    return (subject,) if subject is not None else ()


def _release_sign_offs(state: FactoryState) -> tuple[SignOffRecord, ...]:
    """The recorded Release SignOffRecords the evidence pack chains over (ordered
    by ``Seq`` for a deterministic chain head). The release-approver record is the
    accountable approver-of-record the pack binds (gov §6); chaining over its
    content identity makes a swapped / dropped sign-off tamper-evident."""
    records = [s for s in (state.get("sign_offs") or ()) if s.role == "release-approver"]
    records.sort(key=lambda s: s.seq)
    return tuple(records)


def build_factory_graph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str,
    checkpointer: BaseCheckpointSaver | None = None,
    index: SymbolIndex | None = None,
    max_fix_attempts: int = 3,
    mode: str = MODE_ASSISTANT,
) -> CompiledStateGraph[FactoryState]:
    """Compile the full factory graph (Assistant-mode default; Gated mode opt-in).

    Mounts :func:`build_supervisor_graph` (threaded the SAME ``mode``) as the
    ``factory`` subgraph node, then a MODE-SPECIFIC ``emit`` node, then ``END``
    (``START -> factory -> emit -> END``), and compiles with the injected
    ``checkpointer`` so the run is durable + resumable.

    Parameters
    ----------
    rt:
        The shared :class:`AgentRuntime` (one ``EpochCounter`` + one model egress;
        AR1/L6) threaded into every layer subgraph, the gate nodes, AND the emit
        node's ``Seq`` minting. Its ``adapter`` must be set (the implementation
        layer's Junior requires it).
    runner:
        The :class:`TwoZoneGateRunner` the gate boundaries attest verdicts through
        (verdicts come ONLY from it; L5/GW2).
    resolve / resolve_blob:
        The two materialization seams (GW1a/D-R1-02), forwarded to the supervisor.
    source_dir:
        Base tree cloned into each per-task sandbox (implementation layer; INFRA).
    checkpointer:
        The durable saver attached at compile so the graph is resumable; a
        kill-then-resume re-folds committed writes (including the ``emit``
        ExternalAction) and the keyed ``union_actions`` reducer coalesces the
        re-emitted PR (same idempotency key) — no duplicate PR. ``None`` compiles
        a non-checkpointed graph (callers normally pass a
        :class:`FoundryCheckpointer.saver` or a dev ``MemorySaver``).
    index:
        Context retriever for the Principal Architect (``None`` = greenfield).
    max_fix_attempts:
        Convergence bound forwarded to the supervisor's gate routing.
    mode:
        ``"assistant"`` (DEFAULT) compiles the Phase-1 spine + the bare-PR emit
        (back-compat). ``"gated"`` threads the Gated supervisor (tiering +
        design/release sign-off + assurance) and the evidence-pack-carrying,
        release-gated emit.

    Returns the COMPILED top-level graph.
    """
    gated = mode == MODE_GATED

    supervisor = build_supervisor_graph(
        rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        source_dir=source_dir,
        index=index,
        max_fix_attempts=max_fix_attempts,
        mode=MODE_GATED if gated else MODE_ASSISTANT,
    ).compile()

    def emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Assistant-mode emit: request the bare PR-equivalent external effect.

        Runs AFTER the supervisor's phases complete. Guards on the terminal gate
        PASS (governance §1/§5): only on a clean Spec+Build+Test PASS do we emit
        the ``open_pr`` ExternalAction; a halted/failed run emits NOTHING (no PR).

        The idempotency key is DETERMINISTIC — ``content_digest`` over
        (``run_id``, the integrated artifact's identity) — so a kill-then-resume
        re-derives the SAME key (NOT a wall-clock value, L7) and the keyed
        ``union_actions`` reducer coalesces the re-emission. No real GitHub/network
        call happens here: the action is a request on the ``actions`` channel; the
        outbox relay is the sole component that touches the outside world.
        """
        if not terminal_gates_pass(state):
            # Halted / failed run: do NOT open a PR. Mark the phase only.
            return {"phase": "halted"}
        subject = _pr_subject(state)
        if subject is None:
            # No reviewed artifact to open a PR against — fail-closed (no PR).
            return {"phase": "halted"}

        run_id = config["configurable"]["thread_id"]
        # Process-stable key derived from run + artifact identity (L7 — never
        # hash()/wall clock), so resume re-emits the SAME key and dedups.
        key = content_digest({"run_id": run_id, "artifact_uri": subject.uri})
        action = ExternalAction(
            idempotency_key=f"open_pr:{key}",
            kind="open_pr",
            payload_uri=subject.uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            seq=rt.seq(config, _EMIT, "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    def gated_emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Gated-mode emit: a RELEASE that travels with its WORM evidence pack.

        Runs AFTER the Gated supervisor completes. Fail-closed on
        :func:`gated_release_admitted` — every required gate (terminal +
        tier-scaled assurance) PASS AND an admitted Release sign-off
        (``release_allowed``). A halted (vulnerable-change latch / bound spent /
        blocked release) run emits NOTHING — no PR, no evidence-pack release
        (governance §1/§5/§6).

        On admission it builds a tamper-evident :class:`EvidencePack`
        (:func:`build_evidence_pack`) over the attested GateResults (the WORM-
        anchored verdicts the release was admitted on), the released artifact
        handles (the integrated FeatureDiff), and the recorded Release
        SignOffRecords — hash-chained + content-addressed. The ``open_pr`` action
        then carries the pack's ``pack_uri`` as its payload, so the release
        TRAVELS WITH its evidence (gov §6). No GitHub/network call: the action is
        a request on the ``actions`` channel.

        The idempotency key folds in the ``pack_uri`` (which is a pure function of
        the attested members, L7) alongside run + subject identity, so resume
        re-derives the SAME key and ``union_actions`` coalesces it — no duplicate
        release PR.
        """
        if not gated_release_admitted(state, config):
            # Not released (halt / vulnerable change / unsigned release): emit
            # NOTHING — no PR, no evidence-pack release.
            return {"phase": "halted"}
        subject = _pr_subject(state)
        if subject is None:
            return {"phase": "halted"}

        gate_results = _attested_terminal_results(state)
        artifacts = _released_artifacts(state)
        sign_offs = _release_sign_offs(state)
        # The pack's ``run_against`` is the ACTUAL materialized tree_sha the gates
        # ran against — read off one of the attested verdicts the pack folds over
        # (they carry the true integrated tree_sha; GW2 binds them all to ONE tree),
        # NOT a config-supplied state_sha LABEL. So the pack's stated tree is HONEST:
        # it matches exactly what was gated (M2-R1-02). Fall back to the config
        # state_sha only if (impossibly, post-admission) no verdict carries one.
        run_against = _attested_run_against(gate_results) or state_sha(config)
        pack = build_evidence_pack(
            run_against=run_against,
            gate_results=gate_results,
            artifacts=artifacts,
            sign_offs=sign_offs,
            # The pinned scanner-config revision the release was assured under is
            # READ off an attested verdict (the runner stamps it on every
            # GateResult; AS4) — never the runner's private attribute.
            scanner_config_rev=_scanner_config_rev(gate_results),
            seq=rt.seq(config, _EMIT, "evidence_pack:release"),
        )

        run_id = config["configurable"]["thread_id"]
        # Process-stable key (L7): run + the (content-addressed) subject identity
        # ONLY — the SAME inputs the assistant key uses, so resume re-derives the
        # SAME key and union_actions coalesces the release PR. The pack_uri is
        # DELIBERATELY excluded from the key: it folds in the gate results'
        # content-identities, whose Seq is bumped on a resume re-attestation, so a
        # pack_uri-keyed action would NOT coalesce (it would duplicate the PR).
        # The pack still TRAVELS as the payload; the key only needs to be stable.
        key = content_digest({"run_id": run_id, "artifact_uri": subject.uri})
        action = ExternalAction(
            idempotency_key=f"open_pr:{key}",
            kind="open_pr",
            # The release travels WITH its evidence: the PR payload is the
            # content-addressed evidence-pack handle (gov §6).
            payload_uri=pack.pack_uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            gate_bearing=True,  # a release PR is gate-bearing (never path-3 dedup)
            seq=rt.seq(config, _EMIT, "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    g: StateGraph[FactoryState] = StateGraph(FactoryState)
    g.add_node(_FACTORY, supervisor)
    g.add_node(_EMIT, gated_emit if gated else emit)
    g.add_edge(START, _FACTORY)
    g.add_edge(_FACTORY, _EMIT)
    g.add_edge(_EMIT, END)

    return g.compile(checkpointer=checkpointer)
