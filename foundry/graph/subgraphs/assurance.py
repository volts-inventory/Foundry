r"""Assurance subgraph + the reviewer-driven evaluator-optimizer rework loop.

(architecture.md §3/§4, agents.md Layer 5, governance §1/§1e/§3, roadmap Phase 2 —
"rework loops terminate within caps", implementation-plan §5.1)

This is the Phase-2 (Gated mode) ASSURANCE subgraph — the fourth-and-a-half
mountable ``StateGraph(FactoryState)`` the supervisor sequences after the M1
build/test boundary. It composes the three Layer-5 security agents (advisory
triage authoring) → the reviewer/critic evaluator → the deterministic assurance
gate node, and wraps the whole thing in a BOUNDED evaluator-optimizer rework
loop::

    START
      -> appsec_engineer          # Layer 5: ThreatModel + advisory security triage
      -> audit_compliance         # Layer 5: AuditFinding + advisory audit triage
      -> supply_chain_reviewer    # Layer 5: SupplyChainReport + advisory triage
      -> reviewer_critic          # the EVALUATOR — request-changes | approve verdict
      -> assurance_gate           # [Security/Audit/Supply-chain/E2E] via the runner
         --(advance)--> END
         --(fix)-----> assurance_fix -> assurance_gate   (BOUNDED rework iteration)
         --(halt)----> END

Trust model (the load-bearing invariants — all inherited, none re-implemented):

* **Advisory-only agents (LEARNINGS L5/AR4/SEC1-4).** The three security agents
  author ``GateTriage`` (advisory) + content-addressed bodies; NONE mints a
  ``GateResult``. The reviewer/critic is the ONLY agent that touches ``gates``,
  and only via ``rt.proxy.judge`` (the PROXY stamps the binding; the agent holds
  no key — Q1/Q2). The assurance gate node forwards ONLY runner-attested verdicts
  (``run_assurance_gates``; the verdict comes from the ``TwoZoneGateRunner``,
  never from this module — L5/AW1).

* **Attested-only advancement.** ``route_after_assurance`` advances ONLY when
  every tier-relevant assurance gate has an attested PASS verdict on the trust-
  filtered ``gates`` channel AND the reviewer's proxy-stamped verdict is an
  approve (not a request-changes). An agent-minted verdict was dropped by
  ``merge_gates`` before routing ever sees it (AW1/AW3), so it can never gate
  advancement. A KNOWN-VULNERABLE change → an attested ``security`` FAIL → no
  advance (it latches stable-FAIL → halt; AW3).

* **The rework loop TERMINATES (roadmap Phase 2 cap).** A ``request-changes``
  review OR a gate FAIL/ERROR routes to a bounded ``fix`` iteration that re-runs
  ONLY the re-verification-scoped gates, then loops back to ``assurance_gate``.
  The convergence bound lives on ``BudgetLedger.fix_attempts_since_all_green``
  (single-writer ``budget``), BUMPED ONCE PER NON-ADVANCING iteration by the gate
  node and READ POST-FOLD by the conditional edge in the SAME superstep (the
  EXACT supervisor mechanism, SUP4). After ``max_fix_attempts`` non-advancing
  iterations the router returns ``halt`` — the loop cannot spin forever. A
  stable-FAIL latch also halts at once (AW4).

* **Re-verification is change-proportional (gov §1f / RV1-5).** On a rework
  iteration the gate node re-runs only the gates whose dependency scope the
  change can affect (``reverification_plan``), reusing the prior attested verdict
  for the rest — so a rework does NOT blindly re-run the full assurance family.

The builder holds NO agent logic — only topology (AR3). It returns the
``StateGraph`` UNCOMPILED (SG4): the supervisor owns ``.compile()`` + the shared
checkpointer; a standalone test compiles it itself. Every node is wrapped in a
thin concrete ``def`` for ``add_node`` (the ``NodeFn`` ``Callable`` alias trips
``add_node``'s overload resolution; SG1/SG2/IM5 — no ``cast``, no private import).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.quality import make_reviewer_critic_node
from foundry.agents.security import (
    make_appsec_engineer_node,
    make_audit_compliance_node,
    make_supply_chain_reviewer_node,
)
from foundry.graph.assurance_wiring import (
    ADVANCE,
    FIX,
    HALT,
    assurance_gate_set,
    route_after_assurance,
    run_assurance_gates,
)
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import merge_gates

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph import gate_wiring
    from foundry.schemas.governance import TierDecision

    # The SAME two materialization seams the assurance wiring uses (AW5/D-R1-02):
    # both are TYPE_CHECKING-only there, so we mirror the aliases locally rather
    # than import them at runtime (importing at module top raises ImportError at
    # collection). ``resolve`` reconstitutes a TYPED artifact body from its handle;
    # ``resolve_blob`` fetches the raw diff BODY keyed on its ``cas://`` uri.
    ArtifactResolver = gate_wiring.ArtifactResolver
    BlobResolver = gate_wiring.BlobResolver

__all__ = ["build_assurance_subgraph", "rework_scoped_gates"]

# Node ids match the agent node_ids (so Seq.node_id == the graph node name; AR2).
_APPSEC = "appsec_engineer"
_AUDIT = "audit_compliance"
_SUPPLY_CHAIN = "supply_chain_reviewer"
_REVIEWER = "reviewer_critic"
_ASSURANCE_GATE = "assurance_gate"
_ASSURANCE_FIX = "assurance_fix"

# The reviewer/critic verdict gate name (a JUDGMENT_VERDICTS gate; reducers.py).
_REVIEW_GATE = "review"


# ---- reviewer verdict read (the evaluator side of the loop) -----------------


def _latest_review(state: FactoryState) -> Any:
    """The newest-by-``Seq`` ``review`` verdict on the trust-filtered ``gates``.

    The channel was already trust-filtered by ``merge_gates`` (only the proxy-
    stamped reviewer verdict survived — the agent could not have signed it; L5/Q2),
    so a ``review`` verdict here is genuinely the reviewer's proxy-stamped one.
    Newest by ``Seq`` so a rework iteration's re-review (bumped epoch) wins.
    """
    best: Any = None
    for g in state.get("gates") or ():
        if g.name != _REVIEW_GATE:
            continue
        if best is None or g.seq > best.seq:
            best = g
    return best


def _review_requests_changes(state: FactoryState) -> bool:
    """True iff the reviewer's proxy-stamped verdict is a request-changes (FAIL).

    A MISSING review verdict is treated as "needs review" (fail-closed: never
    advance past the evaluator on an absent verdict). An attested approve (PASS)
    is the only thing that does NOT request changes.

    LOAD-BEARING (H3 negative latch): the fail-closed-on-missing branch is what
    makes "an approve cannot override a latched request-changes" hold. Once the
    judgment-commit step latches a request-changes (``_commit_judgment_ledger``),
    ``merge_gates`` DROPS that verdict (and any later approve at the same key) from
    the channel, so ``_latest_review`` returns None here — which MUST route as
    request-changes. Do NOT change this to fail-open on a missing verdict: that
    would let a latched (rejected) review silently advance. The negative-latch
    no-override invariant is asserted end-to-end in test_agents_quality.
    """
    review = _latest_review(state)
    if review is None:
        return True  # fail-closed: no evaluator verdict yet -> do not advance
    return review.status is not GateStatus.PASS


# ---- the subgraph router (evaluator verdict + attested gate verdicts) --------


def _route_assurance(
    state: FactoryState,
    config: RunnableConfig,
    *,
    runner: TwoZoneGateRunner,
    tier: TierDecision | None,
    max_fix_attempts: int,
) -> str:
    """Route the assurance boundary on BOTH the evaluator + the attested gates.

    Composes the two halves of the evaluator-optimizer loop:

    * the deterministic assurance gates — via ``route_after_assurance`` (AW4,
      fail-closed: stable-FAIL latch → ``halt``; FAIL/ERROR or a missing relevant
      verdict → bounded ``fix`` then ``halt``; all-PASS → ``advance``); AND
    * the reviewer/critic — a ``request-changes`` verdict forces a rework even if
      every deterministic gate passed (the human-proxy evaluator can reject a
      change the machine gates admit).

    A ``request-changes`` never SHORTCUTS the convergence bound: it routes to the
    SAME bounded ``fix`` (and ``halt`` once the bound is spent), so the loop
    terminates regardless of which evaluator (gate or reviewer) is unsatisfied.
    """
    gate_decision = route_after_assurance(
        state, config, records=runner.records, tier=tier, max_fix_attempts=max_fix_attempts
    )
    # A latch / spent bound is terminal regardless of the reviewer.
    if gate_decision == HALT:
        return HALT
    # The reviewer can force a rework even on all-PASS gates — but still bounded.
    if gate_decision == ADVANCE and _review_requests_changes(state):
        bound_spent = _fix_attempts(state, config) >= max_fix_attempts
        return HALT if bound_spent else FIX
    return gate_decision


def _fix_attempts(state: FactoryState, config: RunnableConfig) -> int:
    """The convergence bound's spent attempts (mirrors assurance_wiring)."""
    budget = state.get("budget")
    if budget is not None:
        return int(budget.fix_attempts_since_all_green)
    configurable = config.get("configurable") or {}
    return int(configurable.get("fix_attempts", 0))


# ---- re-verification scoping (change-proportional rework; gov §1f) -----------


def rework_scoped_gates(
    state: FactoryState,
    *,
    runner: TwoZoneGateRunner,
    tier: TierDecision | None,
) -> tuple[str, ...]:
    """The assurance gates a rework iteration must RE-RUN — change-proportional.

    On a first pass there is no prior verdict to reuse, so every tier-relevant
    gate runs. On a rework iteration we scope the re-run set with
    :func:`reverification_plan` (gov §1f / RV1-5): a gate whose dependency scope
    is disjoint from the change set AND whose prior attested verdict still matches
    its reuse key is REUSED (not re-run); only the affected gates re-run. The
    return is the ordered tuple of gate NAMES to re-run (a subset of
    ``assurance_gate_set(tier)``), so a caller can re-run only those.

    This is the change-proportional facet the loop exercises: a rework does not
    blindly re-run the full assurance family. The ``changed`` set is derived by
    the caller from the integrated trees; here we expose the SCOPING contract so
    the subgraph (and its tests) can assert the rework re-runs only the scoped
    gates rather than the whole set.
    """
    from foundry.graph.assurance_wiring import _assurance_gates  # local: avoid cycle
    from foundry.graph.reverification import changed_scope, reverification_plan

    gate_impls = [gate for _node_id, gate in _assurance_gates(tier, allow_build_changes=False)]
    prior = list(state.get("gates") or ())
    # No prior attested verdicts -> nothing to reuse -> re-run the whole set.
    if not prior:
        return assurance_gate_set(tier)

    # On a rework iteration the integrated tree has not yet been re-materialized
    # (the fix is applied upstream); the scope is the empty delta against the last
    # verified tree EXCEPT for the gates whose verdict was not a PASS — those are
    # always re-run (their inputs are exactly what the rework must change). We
    # express that via the plan: a gate with no reusable PASS prior re-runs.
    changed = changed_scope({}, {})  # no file delta known at this seam
    plan = reverification_plan(
        prior,
        changed,
        gates=gate_impls,
        runner=runner,
        new_state_sha=_latest_run_against(state),
        # The current scanner config rev — read off a prior attested verdict (the
        # runner stamps it on every GateResult; it is the runner's own rev, AW1).
        scanner_config_rev=_scanner_config_rev(prior),
    )
    rerun = set(plan.rerun_gates)
    # Always re-run any tier-relevant gate whose newest prior verdict is not a
    # PASS (FAIL/ERROR): the rework targets exactly those (fail-closed).
    for name in assurance_gate_set(tier):
        if not _has_passing_prior(state, name):
            rerun.add(name)
    return tuple(name for name in assurance_gate_set(tier) if name in rerun)


def _scanner_config_rev(prior: list[Any]) -> str:
    """The runner's current scanner config rev — read off a prior attested verdict.

    The runner stamps its ``scanner_config_rev`` on every ``GateResult`` it signs
    (AW1), so a prior attested verdict carries the current rev without reaching
    through the runner's private state. Empty when there is no prior verdict.
    """
    return str(prior[0].scanner_config_rev) if prior else ""


def _latest_run_against(state: FactoryState) -> str:
    """The integrated digest the prior assurance verdicts bound to (newest-by-Seq)."""
    best: Any = None
    for g in state.get("gates") or ():
        if best is None or g.seq > best.seq:
            best = g
    return str(best.run_against) if best is not None else ""


def _has_passing_prior(state: FactoryState, name: str) -> bool:
    """True iff the newest-by-Seq prior verdict for ``name`` is an attested PASS."""
    best: Any = None
    for g in state.get("gates") or ():
        if g.name != name:
            continue
        if best is None or g.seq > best.seq:
            best = g
    return best is not None and best.status is GateStatus.PASS


# ---- builder ----------------------------------------------------------------


def build_assurance_subgraph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str | None = None,
    tier: TierDecision | None = None,
    max_fix_attempts: int = 3,
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED assurance subgraph over :class:`FactoryState`.

    Composes the three Layer-5 security agents (advisory triage authoring) → the
    reviewer/critic evaluator → the deterministic assurance gate node, wired into
    a BOUNDED evaluator-optimizer rework loop (see the module docstring for the
    topology). Returns the ``StateGraph`` UNCOMPILED (SG4) — the supervisor owns
    ``.compile()`` + the shared checkpointer; a standalone test compiles it itself.

    Parameters
    ----------
    rt:
        Shared :class:`AgentRuntime` threaded into every node (one ``EpochCounter``
        + one model egress; AR1/L6).
    runner:
        The :class:`TwoZoneGateRunner` the assurance gate node runs Security /
        Audit / Supply-chain / E2E through — verdicts come ONLY from it (L5/AW1).
        It holds the zone-B signer + the cross-fork ``GlobalRecords`` (the
        stable-FAIL latch routing reads via the public ``runner.records``).
    resolve / resolve_blob:
        The two materialization seams (AW5/D-R1-02): ``resolve`` reconstitutes a
        TYPED artifact body from its content-addressed handle; ``resolve_blob``
        fetches the raw diff BODY keyed on its ``cas://`` uri (prod:
        ``sandbox.get_blob``). Threaded into the assurance gate node's
        materialization.
    source_dir:
        Accepted for signature parity with the other subgraph builders (the
        assurance gates materialize a fresh tree from the content-addressed
        handles, so they need no on-disk source clone); unused here.
    tier:
        The :class:`TierDecision` scaling assurance DEPTH (gov §1e). Read from
        ``state['risk_tier']`` at run time if present (single-writer, set by
        triage tiering); the builder-time ``tier`` is the fallback default.
    max_fix_attempts:
        Convergence bound: a non-advancing iteration (gate FAIL/ERROR OR a
        reviewer request-changes) may route ``fix`` at most this many times
        before the router returns ``halt`` — so the rework loop can never spin
        forever (roadmap Phase 2 cap; tracked on
        ``budget.fix_attempts_since_all_green``, SUP4).
    """
    _ = source_dir  # parity-only (see docstring)
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    appsec = make_appsec_engineer_node(rt)
    audit = make_audit_compliance_node(rt)
    supply_chain = make_supply_chain_reviewer_node(rt)
    reviewer = make_reviewer_critic_node(rt)

    def _state_tier(state: FactoryState) -> TierDecision | None:
        # The single-writer risk_tier channel (triage tiering, gov §1e) is the
        # source of truth; fall back to the builder-time default.
        return state.get("risk_tier") or tier

    # -- advisory security agents (L5: GateTriage only, never a GateResult) ----
    def _appsec(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return appsec(state, config)

    def _audit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return audit(state, config)

    def _supply_chain(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return supply_chain(state, config)

    # -- the evaluator: reviewer/critic proxy-stamped verdict (L5/Q2) ---------
    def _reviewer(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return reviewer(state, config)

    # -- the deterministic assurance gate node --------------------------------
    # FIRST pass: run the FULL tier-scaled assurance family via the runner
    # (verdicts ONLY from it; L5/AW1). REWORK iteration (prior verdicts present):
    # re-run ONLY the re-verification-scoped gates (gov §1f / RV1-5) — a
    # change-proportional rework, never a blind full re-run. Then BUMP the
    # convergence counter iff its own boundary is not advancing — read POST-FOLD
    # by the conditional edge in the SAME superstep (the supervisor mechanism,
    # SUP4) so the loop converges: fix N times, halt.
    def _assurance(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        run_tier = _state_tier(state)
        prior = list(state.get("gates") or ())
        if prior:
            out = _run_scoped(state, config, run_tier)
        else:
            out = run_assurance_gates(
                state,
                config,
                rt=rt,
                runner=runner,
                resolve=resolve,
                resolve_blob=resolve_blob,
                workdir=Path(tempfile.mkdtemp(prefix="foundry_assurance_")),
                tier=run_tier,
            )
        out["phase"] = _ASSURANCE_GATE
        _bump_on_non_advance(state, config, out, run_tier)
        return out

    def _run_scoped(
        state: FactoryState, config: RunnableConfig, run_tier: TierDecision | None
    ) -> dict[str, Any]:
        """Re-run ONLY the re-verification-scoped assurance gates (gov §1f).

        Materializes the integrated tree ONCE, then runs only the gates
        :func:`rework_scoped_gates` selected (the affected subset); the rest keep
        their prior attested verdict (already on ``state['gates']``, folded
        forward by ``merge_gates``). Verdicts still come ONLY from the runner.
        """
        from foundry.graph.assurance_wiring import (
            _assurance_gates,
            materialize_assurance_workdir,
        )

        scoped = set(rework_scoped_gates(state, runner=runner, tier=run_tier))
        workdir = Path(tempfile.mkdtemp(prefix="foundry_assurance_"))
        run_against = materialize_assurance_workdir(
            state, workdir, resolve=resolve, resolve_blob=resolve_blob
        )
        results = []
        for node_id, gate in _assurance_gates(
            run_tier, allow_build_changes=False, seal_key=runner.seal_key
        ):
            if gate.name not in scoped:
                continue  # unaffected -> reuse the prior attested verdict (no re-run)
            seq = rt.seq(config, node_id, f"verdict:{gate.name}")
            results.append(runner.run(gate, run_against, seq, workdir))
        return {"gates": results}

    def _bump_on_non_advance(
        state: FactoryState,
        config: RunnableConfig,
        out: dict[str, Any],
        run_tier: TierDecision | None,
    ) -> None:
        # The in-node bump decision MUST be derived from the SAME post-fold
        # ``gates`` view the conditional edge routes on (E-R1-01/SUP4): fold the
        # prior channel with the node's freshly attested delta through the REAL
        # ``merge_gates`` reducer, then route the same way the edge will.
        post: FactoryState = {
            "gates": merge_gates(state.get("gates"), out.get("gates")),
            "budget": state.get("budget") or BudgetLedger(),
        }
        decision = _route_assurance(
            post, config, runner=runner, tier=run_tier, max_fix_attempts=max_fix_attempts
        )
        if decision == FIX:
            ledger = state.get("budget") or BudgetLedger()
            out["budget"] = ledger.model_copy(
                update={
                    "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + 1
                }
            )

    # -- the bounded re-verification (fix) node -------------------------------
    # The counter is already bumped by the gate node; the fix node only marks the
    # phase + edges back to the gate to re-run the re-verification-scoped gates.
    # (Kept distinct so the rendered topology shows the rework path explicitly.)
    def _fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _ASSURANCE_FIX}

    # -- the conditional router (advance / fix / halt) ------------------------
    def _route(state: FactoryState, config: RunnableConfig) -> str:
        return _route_assurance(
            state,
            config,
            runner=runner,
            tier=_state_tier(state),
            max_fix_attempts=max_fix_attempts,
        )

    # -- register nodes (concrete defs; SG1/SG2/IM5 mypy seam) ----------------
    g.add_node(_APPSEC, _appsec)
    g.add_node(_AUDIT, _audit)
    g.add_node(_SUPPLY_CHAIN, _supply_chain)
    g.add_node(_REVIEWER, _reviewer)
    g.add_node(_ASSURANCE_GATE, _assurance)
    g.add_node(_ASSURANCE_FIX, _fix)

    # -- wire the SOP chain + the bounded rework loop -------------------------
    # D7 fan-out: the three security AUTHORS (appsec/audit/supply_chain) are
    # independent — each reads the integrated tree and writes a disjoint
    # advisory artifact + gate_triage; none consumes another's output. Run them in
    # ONE superstep; the reviewer (3 incoming edges) is the barrier that waits for
    # all three before it runs, then the gate. Concurrency-safe: locked
    # EpochCounter + GIL-atomic CAS writes (see LATENCY_PARALLELISM_DESIGN).
    g.add_edge(START, _APPSEC)
    g.add_edge(START, _AUDIT)
    g.add_edge(START, _SUPPLY_CHAIN)
    g.add_edge(_APPSEC, _REVIEWER)
    g.add_edge(_AUDIT, _REVIEWER)
    g.add_edge(_SUPPLY_CHAIN, _REVIEWER)
    g.add_edge(_REVIEWER, _ASSURANCE_GATE)

    # The assurance boundary: advance -> END; fix -> the bounded rework iteration
    # (re-run the re-verification-scoped gates); halt -> END. The path map lists
    # every legal router return value or compile rejects the route.
    g.add_conditional_edges(
        _ASSURANCE_GATE,
        _route,
        {ADVANCE: END, FIX: _ASSURANCE_FIX, HALT: END},
    )
    g.add_edge(_ASSURANCE_FIX, _ASSURANCE_GATE)  # bounded loop back to the gate

    return g
