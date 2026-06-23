r"""Supervisor — phase routing across the four layer subgraphs (T-M1-13).

(architecture.md §1/§7, agents.md "Triage SOP" + Layer 1-4, governance §1)

The supervisor is the spine that SEQUENCES the four role subgraphs and wires the
deterministic gates at the phase boundaries. It is the only place phase order +
gate-wiring routing live; the layer subgraphs themselves hold no cross-phase
knowledge (each is an UNCOMPILED ``StateGraph`` the supervisor mounts as a node).

Topology (the SOP phase order — agents.md, governance §1 gate boundaries)::

    START
      -> triage              # classify -> route + seed per-route budget
      -> product             # Layer 1 subgraph (PRD + AcceptanceCriterion[])
      -> architecture        # Layer 2 subgraph (SystemDesign + constraints)
      -> spec_gate           # [SPEC gate] criteria(product) x design(arch)
         --(advance)--> implementation
         --(fix)-----> spec_fix -> architecture        (bounded re-verification)
         --(halt)----> END
      -> implementation      # Layer 3 subgraph (Task[] -> FeatureDiff[])
      -> quality             # Layer 4 subgraph (TestPlan/Report/E2E + review)
      -> build_test_gate     # [BUILD + TEST gates] on the integrated tree
         --(advance)--> END
         --(fix)-----> build_test_fix -> implementation   (bounded re-verification)
         --(halt)----> END

Gate nodes (``spec_gate`` / ``build_test_gate``) call the gate-wiring helpers
(:func:`run_spec_gate` / :func:`run_build_test_gates`) — the verdict comes ONLY
from the injected ``TwoZoneGateRunner`` (LEARNINGS L5/GW2); the node merely
forwards the attested ``GateResult`` onto the ``gates`` channel.

Routing at each boundary uses :func:`route_after_gates` (LEARNINGS GW3,
fail-closed): ``advance`` proceeds to the next phase, ``fix`` routes back to the
responsible layer for re-verification within ``max_fix_attempts``, ``halt`` ends
the run (a stable-FAIL latch, or the convergence bound spent). The convergence
bound is read off ``budget.fix_attempts_since_all_green`` (BudgetLedger,
single-writer); a dedicated ``*_fix`` node BUMPS it on each fix iteration so the
loop is bounded — no infinite cycle. (LEARNINGS SUP1.)

This builder returns the UNCOMPILED ``StateGraph`` (mirrors ``skeleton.py`` and
the four subgraph builders, SG4): ``factory_graph`` (T-M1-14) compiles it, adds
the shared checkpointer, and appends the assistant-mode emit node. The supervisor
defines NONE of that here.

The ``add_node`` mypy seam (LEARNINGS SG2/IM5): a factory closure typed as the
``NodeFn`` ``Callable`` alias trips ``add_node``'s overload resolution, so every
node is wrapped in a thin concrete ``def`` (no ``cast``, no private import).

Phase-2 (Gated mode) extension (T-M2-09)
----------------------------------------
``mode="gated"`` selects the Phase-2 flow; ``mode="assistant"`` (the DEFAULT)
keeps the Phase-1 topology byte-for-byte (back-compat). The Gated path ADDS three
boundaries to the Phase-1 spine, never relaxing its convergence bound or
attested-only advancement::

    triage (classify_tier -> risk_tier + mode)
      -> product
      -> architecture
      -> design_sign_off     # [Design gate] record a SignOffRecord (always)
      -> spec_gate           # (unchanged integrity boundary)
      -> implementation
      -> quality
      -> build_test_gate     # (unchanged integrity boundary)
      -> assurance           # build_assurance_subgraph (sec/audit/supply + reviewer)
         --(advance)--> release_sign_off  # [Release gate] record + release_allowed
         --(halt)----> END                # vulnerable change caught -> NO release
      -> release_sign_off
         --(release_allowed)--> END        # fail-closed: no record => no emit
         --(blocked)---------> END

* TIERING (gov §1e): triage calls :func:`classify_tier`, writing the
  ``risk_tier`` (``TierDecision``) single-writer channel + ``mode``. The tier
  scales the assurance subgraph's DEPTH (passed through), NEVER integrity.
* DESIGN / RELEASE SIGN-OFF (gov §1/§6): each boundary ALWAYS records a
  ``SignOffRecord`` (self-judge stamp, bound to the artifact + ``state_sha`` +
  a process-stable nonce) — the always-recorded invariant. Advancement past the
  Release boundary to emit is gated by :func:`release_allowed` (fail-closed: no
  recorded approved sign-off ⇒ no release).
* ASSURANCE (AS1/AW1): the assurance subgraph routes ``advance``/``halt`` like
  the other gate boundaries; a known-vulnerable change is caught there (attested
  FAIL → latch → ``halt``), so there is NO release-sign-off path to emit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation.senior_engineer import DESIGN_KIND
from foundry.governance.budget_controller import BudgetCaps, BudgetController
from foundry.governance.release_anchors import (
    evidence_hash,
    newest_artifact_uri,
    newest_release_sign_off,
    sign_off_nonce,
    state_sha,
)
from foundry.governance.sign_off import (
    record_design_sign_off,
    record_release_sign_off,
    release_allowed,
)
from foundry.governance.tiering import classify_tier
from foundry.graph.assurance_wiring import ADVANCE as ASSURANCE_ADVANCE
from foundry.graph.assurance_wiring import HALT as ASSURANCE_HALT
from foundry.graph.assurance_wiring import route_after_assurance
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    SPEC_BOUNDARY,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
)
from foundry.graph.subgraphs.architecture import build_architecture_subgraph
from foundry.graph.subgraphs.assurance import build_assurance_subgraph
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.graph.subgraphs.product import build_product_subgraph
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.schemas.base import BudgetLedger
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import merge_gates

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver

__all__ = [
    "DEFAULT_ROUTE_CAPS_MICROUSD",
    "MODE_ASSISTANT",
    "MODE_AUTONOMOUS",
    "MODE_GATED",
    "ROUTES",
    "build_supervisor_graph",
    "classify_request",
]

# ---- supervisor mode --------------------------------------------------------

MODE_ASSISTANT = "assistant"  # Phase-1 flow (default; back-compat)
MODE_GATED = "gated"  # Phase-2 flow: tiering + design/release sign-off + assurance
# Phase-4 (Autonomous mode, T-M4-10): the SAME trusted Gated assurance spine
# (tiering + assurance gates + the hardened runner + discriminator), but the
# RELEASE boundary is NOT a human/recorded sign-off — it is the TRUST ROOT
# (build → admit → deploy, hands-off). The deterministic gates + the assurance
# family + the convergence bound are inherited UNCHANGED from Gated mode; the
# only difference is the terminal release gate: deploy-time ADMISSION, not a
# sign-off. So the supervisor phase spine is byte-identical to Gated; the
# difference lives entirely in the factory_graph emit node.
MODE_AUTONOMOUS = "autonomous"

# ---- supervisor node ids ----------------------------------------------------

_TRIAGE = "triage"
_PRODUCT = "product"
_ARCHITECTURE = "architecture"
_SPEC_GATE = "spec_gate"
_IMPLEMENTATION = "implementation"
_QUALITY = "quality"
_BUILD_TEST_GATE = "build_test_gate"
# The bounded re-verification nodes: bump the convergence counter, then edge
# back to the responsible layer (the layer that must regenerate the tree).
_SPEC_FIX = "spec_fix"
_BUILD_TEST_FIX = "build_test_fix"
# ---- Phase-2 (Gated mode) boundary node ids ---------------------------------
_DESIGN_SIGN_OFF = "design_sign_off"
_ASSURANCE = "assurance"
_RELEASE_SIGN_OFF = "release_sign_off"

# ---- triage classification (Phase-1 deterministic classifier) ---------------

ROUTES = ("greenfield", "evolution", "bugfix", "spike")

# Default per-route absolute budget cap (integer micro-USD; refined later by the
# Eng/Program Manager on routes that visit PLAN — agents.md Triage SOP). A
# bugfix is the cheapest route; a greenfield the most expensive.
DEFAULT_ROUTE_CAPS_MICROUSD: dict[str, int] = {
    "greenfield": 50_000_000,
    "evolution": 30_000_000,
    "bugfix": 10_000_000,
    "spike": 8_000_000,
}

# Substring markers that classify a request string (first match wins, in order).
# Deterministic + cheap (Phase 1); a learned classifier is a later phase. The
# DEFAULT is greenfield (the most capable route — never under-provision).
_ROUTE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bugfix", ("bug", "fix ", "regression", "broken", "crash", "defect", "hotfix")),
    ("spike", ("spike", "investigate", "research", "explore", "prototype", "feasibility")),
    ("evolution", ("add ", "extend", "evolve", "refactor", "migrate", "change ", "update ")),
)


def classify_request(request: str) -> str:
    """Classify a raw request string into one of :data:`ROUTES` (deterministic).

    A simple substring classifier (Phase 1, agents.md Triage SOP): bugfix /
    spike / evolution markers win in order; otherwise the request is treated as
    a ``greenfield`` build (the safe default — the most capable route). The
    classification is purely a function of the request text, so it is stable
    across a kill-then-resume.
    """
    text = request.lower()
    for route, markers in _ROUTE_MARKERS:
        if any(marker in text for marker in markers):
            return route
    return "greenfield"


def _seed_budget(route: str) -> BudgetLedger:
    """Seed the per-route :class:`BudgetLedger` cap via the Budget Controller.

    The controller is the single budget authority (governance §7); we use it to
    derive the per-route absolute cap, then stamp it onto the single-writer
    ``budget`` channel as the initial ledger (zero spend, zero fix attempts).
    """
    cap = DEFAULT_ROUTE_CAPS_MICROUSD.get(route, DEFAULT_ROUTE_CAPS_MICROUSD["greenfield"])
    controller = BudgetController(BudgetCaps(total_microusd=cap))
    return BudgetLedger(cap_microusd=controller.caps.total_microusd)


# ---- Phase-2 sign-off anchors (always-recorded; gov §1/§6) ------------------
# The four what-you-sign anchor derivations + the newest-release-sign-off lookup
# now live in the shared PUBLIC module ``foundry.governance.release_anchors`` —
# a single source of truth imported by BOTH this supervisor and the gated emit's
# release re-check (factory_graph), so the two can never drift (M2-R1-01). They
# are imported above as ``newest_artifact_uri`` / ``state_sha`` / ``sign_off_nonce``
# / ``evidence_hash`` / ``newest_release_sign_off``.


# ---- builder ----------------------------------------------------------------


def build_supervisor_graph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str,
    index: SymbolIndex | None = None,
    max_fix_attempts: int = 3,
    mode: str = MODE_ASSISTANT,
    discriminator_factory: DiscriminatorFactory | None = None,
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED supervisor graph over :class:`FactoryState`.

    Parameters
    ----------
    rt:
        Shared :class:`AgentRuntime` threaded into every layer subgraph + gate
        node (one ``EpochCounter`` + one model egress; AR1/L6). Its ``adapter``
        must be set (the implementation layer's Junior requires it).
    runner:
        The :class:`TwoZoneGateRunner` the gate nodes run Spec / Build / Test
        through — verdicts come ONLY from it (L5/GW2). Holds the zone-B signer +
        the cross-fork :class:`GlobalRecords` (the stable-FAIL latch routing
        reads).
    resolve / resolve_blob:
        The two materialization seams (GW1a/D-R1-02): ``resolve`` reconstitutes
        a TYPED artifact body (AcceptanceCriterion / FeatureDiff / SystemDesign /
        OwnershipMap) from its content-addressed handle; ``resolve_blob`` fetches
        a raw diff BODY keyed on its ``cas://`` uri (prod: ``sandbox.get_blob``).
        Threaded into both the implementation/quality subgraphs and the gate
        nodes.
    source_dir:
        Base tree cloned into each per-task sandbox (the implementation layer's
        Junior; INFRA).
    index:
        Context retriever for the Principal Architect (``None`` = greenfield,
        empty retrieval; AC1).
    max_fix_attempts:
        Convergence bound: a gate boundary may route ``fix`` (re-verification)
        at most this many times before ``route_after_gates`` returns ``halt``.
        Tracked on ``budget.fix_attempts_since_all_green`` (bumped by the
        ``*_fix`` node), so the rework loop can never spin forever.
    mode:
        ``"assistant"`` (DEFAULT) builds the Phase-1 topology unchanged
        (back-compat). ``"gated"`` builds the Phase-2 flow: triage tiering
        (``classify_tier`` → ``risk_tier``), a Design sign-off boundary after
        architecture, the assurance subgraph after build+test, and a Release
        sign-off boundary (``release_allowed``-gated, fail-closed) before emit.
        ``"autonomous"`` (Phase-4, T-M4-10) builds the SAME assurance spine as
        Gated (tiering + the Design sign-off boundary + the assurance subgraph
        + the convergence bound — integrity is never relaxed), so the phase
        topology is byte-identical to Gated; the difference is the RELEASE
        boundary, which is the TRUST ROOT (build → admit → deploy, hands-off)
        rather than a human/recorded sign-off — and that lives entirely in the
        factory_graph autonomous emit node, NOT here. The Gated/Autonomous paths
        only ADD boundaries — the convergence bound + the attested-only
        advancement are inherited unchanged (gov §1e).
    discriminator_factory:
        OPTIONAL malicious-test defense (A-M4-01). When provided, the
        ``build_test_gate`` node threads it into :func:`run_build_test_gates`, so
        the COMPILED Test gate consults the held-out-mutant + behavioral-invariance
        :class:`~foundry.gates.discriminator.TestSuiteDiscriminator` the factory
        produces — a gamed-green suite that clears the adequacy floor but does not
        kill a held-out mutant FAILs the gate in the SHIPPING graph (not only in
        ``tests/test_phase4_exit.py``). ``None`` (the default) keeps the Phase-1/2
        behavior byte-for-byte (adequacy floor only; Assistant/Gated/Autonomous
        back-compat).

    Returns the ``StateGraph`` UNCOMPILED — ``factory_graph`` (T-M1-14) compiles
    it, attaches the shared checkpointer, and appends the emit node.
    """
    # Autonomous mode reuses the trusted Gated assurance spine verbatim (the
    # phase order, the tiering, the assurance subgraph, the convergence bound).
    # The ONLY difference is the terminal release gate (admission, not sign-off),
    # which is the factory_graph emit node's concern — so the supervisor builds
    # the same graph for both. (T-M4-10.)
    gated = mode in (MODE_GATED, MODE_AUTONOMOUS)
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # -- triage: classify + seed route/budget; gated also tiers (gov §1e) -----
    def triage(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        route = classify_request(state.get("request", ""))
        out: dict[str, Any] = {
            "phase": _TRIAGE,
            "route": route,
            "budget": _seed_budget(route),
            # Record the EXACT mode on the single-writer channel (autonomous is
            # distinct from gated even though it reuses the gated spine).
            "mode": mode if gated else MODE_ASSISTANT,
        }
        if gated:
            # Change-risk tiering (gov §1e): write the risk_tier single-writer
            # channel; the tier scales assurance DEPTH (passed to the assurance
            # subgraph at run time via state['risk_tier']), NEVER integrity.
            tier_seq = rt.seq(config, _TRIAGE, "tier")
            out["risk_tier"] = classify_tier(
                state,
                capabilities=state.get("required_capabilities"),
                seq=tier_seq,
                run_against=state_sha(config),
            )
        return out

    # -- the four layer subgraphs, compiled + mounted as nodes ----------------
    # Each builder returns an UNCOMPILED StateGraph (SG4); we ``.compile()`` each
    # so it can be added as a single node of the supervisor (its internal
    # channel writes fold through FactoryState's reducers on the parent run).
    product = build_product_subgraph(rt).compile()
    architecture = build_architecture_subgraph(rt, index).compile()
    implementation = build_implementation_subgraph(
        rt, source_dir=source_dir, resolve=resolve
    ).compile()
    quality = build_quality_subgraph(rt, resolve=resolve).compile()

    def product_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(product.invoke(state, config))
        out["phase"] = _PRODUCT
        return out

    def architecture_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(architecture.invoke(state, config))
        out["phase"] = _ARCHITECTURE
        return out

    def implementation_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(implementation.invoke(state, config))
        out["phase"] = _IMPLEMENTATION
        return out

    def quality_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(quality.invoke(state, config))
        out["phase"] = _QUALITY
        return out

    # -- gate nodes: run the gates via the runner; verdicts ONLY from it -------
    # The gate node ALSO bumps the convergence counter when its own boundary is
    # not advancing, so ``route_after_gates`` (the conditional edge that fires
    # in the SAME superstep, reading the post-fold state) sees the updated
    # ``fix_attempts`` and halts once the bound is spent. ``budget`` is
    # single-writer; the gate node is its sole writer in that superstep.
    def _bump_on_non_advance(
        state: FactoryState, config: RunnableConfig, out: dict[str, Any], *, boundary: str
    ) -> None:
        # The in-node bump decision MUST be derived from the SAME post-fold
        # ``gates`` view the conditional edge routes on. The edge reads the full
        # ``gates`` channel AFTER LangGraph folds the node's ``out["gates"]``
        # delta through ``merge_gates`` (latest-by-Seq per (name, run_against),
        # trust-filtered). So fold the prior channel (``state["gates"]``) with
        # the node's freshly attested delta through the REAL reducer here —
        # never a hand-assembled subset — so the in-node bump can never diverge
        # from the edge's post-fold read by an unenforced invariant (E-R1-01).
        post: FactoryState = {
            "gates": merge_gates(state.get("gates"), out.get("gates")),
            "budget": state.get("budget") or BudgetLedger(),
        }
        decision = route_after_gates(
            post,
            config,
            boundary=boundary,
            records=runner.records,
            max_fix_attempts=max_fix_attempts,
        )
        if decision == "fix":
            ledger = state.get("budget") or BudgetLedger()
            out["budget"] = ledger.model_copy(
                update={
                    "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + 1
                }
            )

    def _gate_workdir(name: str) -> Path:
        # A fresh materialization tree per gate run (materialize_workdir writes
        # into it from the content-addressed handles; the integrated tree_sha is
        # a pure function of those handles, so a per-run temp dir is fine).
        return Path(tempfile.mkdtemp(prefix=f"foundry_{name}_"))

    def spec_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out = run_spec_gate(
            state,
            config,
            rt=rt,
            runner=runner,
            resolve=resolve,
            resolve_blob=resolve_blob,
            workdir=_gate_workdir("spec_gate"),
        )
        out["phase"] = _SPEC_GATE
        _bump_on_non_advance(state, config, out, boundary=SPEC_BOUNDARY)
        return out

    def build_test_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out = run_build_test_gates(
            state,
            config,
            rt=rt,
            runner=runner,
            resolve=resolve,
            resolve_blob=resolve_blob,
            workdir=_gate_workdir("build_test_gate"),
            discriminator_factory=discriminator_factory,
        )
        out["phase"] = _BUILD_TEST_GATE
        _bump_on_non_advance(state, config, out, boundary=BUILD_TEST_BOUNDARY)
        return out

    # -- bounded re-verification (fix) nodes ----------------------------------
    # The counter is already bumped by the gate node; the fix node just marks the
    # phase and edges back to the responsible layer. (Kept distinct so the
    # rendered topology shows the re-verification path explicitly.)
    def spec_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _SPEC_FIX}

    def build_test_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _BUILD_TEST_FIX}

    # -- conditional routers (advance / fix / halt) ---------------------------
    def _route_spec(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=SPEC_BOUNDARY,
            records=runner.records,
            max_fix_attempts=max_fix_attempts,
        )

    def _route_build_test(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=BUILD_TEST_BOUNDARY,
            records=runner.records,
            max_fix_attempts=max_fix_attempts,
        )

    # -- Phase-2 (Gated mode) boundary nodes ----------------------------------
    # Built unconditionally (cheap) but wired ONLY when gated; the assistant
    # topology stays byte-identical to Phase-1 (back-compat).

    # The assurance subgraph (sec/audit/supply-chain + reviewer evaluator-
    # optimizer loop, AS1). The tier scales its DEPTH at run time off
    # state['risk_tier'] (set by triage tiering); reuses the SAME runner +
    # materialization seams as the M1 gate nodes (verdicts ONLY from the runner).
    assurance = build_assurance_subgraph(
        rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        source_dir=source_dir,
        max_fix_attempts=max_fix_attempts,
    ).compile()

    def assurance_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(assurance.invoke(state, config))
        out["phase"] = _ASSURANCE
        return out

    def _route_assurance(state: FactoryState, config: RunnableConfig) -> str:
        # The subgraph already converged (advance) or latched/spent-bound (halt)
        # internally; the supervisor reads its outcome off the post-fold gates +
        # the cross-fork latch, exactly like the M1 boundaries (AS3/AW4). A
        # halt (a known-vulnerable change caught, or the bound spent) routes to
        # END — there is NO release-sign-off path to emit from a halted run.
        decision = route_after_assurance(
            state,
            config,
            records=runner.records,
            tier=state.get("risk_tier"),
            max_fix_attempts=max_fix_attempts,
        )
        return ASSURANCE_ADVANCE if decision == ASSURANCE_ADVANCE else ASSURANCE_HALT

    # The Design sign-off boundary (gov §1 Design row): ALWAYS record a Design
    # SignOffRecord (self-judge stamp, bound to the design artifact + state_sha +
    # a process-stable nonce). In the self-judge profile this is the recorded-
    # but-no-op HITL; the RECORD is always made (the testable invariant).
    def design_sign_off(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        subject_uri = newest_artifact_uri(state, DESIGN_KIND)
        sha = state_sha(config)
        nonce = sign_off_nonce(config, _DESIGN_SIGN_OFF, subject_uri, sha)
        ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
        seq = rt.seq(config, _DESIGN_SIGN_OFF, "sign_off:design")
        record = record_design_sign_off(
            subject_artifact_uri=subject_uri,
            state_sha=sha,
            presented_evidence_hash=ev_hash,
            server_nonce=nonce,
            approver="self-judge",
            role="design-approver",
            profile=rt.proxy.profile,
            seq=seq,
        )
        return {"phase": _DESIGN_SIGN_OFF, "sign_offs": [record]}

    # The Release sign-off boundary (gov §1 Release row, §6): ALWAYS record a
    # Release SignOffRecord, then gate advancement to emit with release_allowed
    # — fail-closed (no recorded approved sign-off binding THIS release ⇒ no
    # release). The record is made regardless; only ADVANCEMENT is gated.
    def release_sign_off(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        subject_uri = newest_artifact_uri(state, DESIGN_KIND)
        sha = state_sha(config)
        nonce = sign_off_nonce(config, _RELEASE_SIGN_OFF, subject_uri, sha)
        ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
        seq = rt.seq(config, _RELEASE_SIGN_OFF, "sign_off:release")
        record = record_release_sign_off(
            subject_artifact_uri=subject_uri,
            state_sha=sha,
            presented_evidence_hash=ev_hash,
            server_nonce=nonce,
            approver="self-judge",
            role="release-approver",
            profile=rt.proxy.profile,
            seq=seq,
        )
        return {"phase": _RELEASE_SIGN_OFF, "sign_offs": [record]}

    def _route_release(state: FactoryState, config: RunnableConfig) -> str:
        # Fail-closed release admission (gov §1/§5): advance to emit ONLY if a
        # recorded, approved sign-off binds THIS exact release (subject +
        # state_sha + evidence + nonce). The record is re-derived against the
        # SAME anchors the node bound, so a missing/!approved/mismatched sign-off
        # blocks emit. (No record ⇒ not released.)
        subject_uri = newest_artifact_uri(state, DESIGN_KIND)
        sha = state_sha(config)
        nonce = sign_off_nonce(config, _RELEASE_SIGN_OFF, subject_uri, sha)
        ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
        record = newest_release_sign_off(state)
        ok = release_allowed(
            record,
            subject_artifact_uri=subject_uri,
            state_sha=sha,
            presented_evidence_hash=ev_hash,
            server_nonce=nonce,
        )
        return ASSURANCE_ADVANCE if ok else ASSURANCE_HALT

    # -- register nodes (concrete defs; SG2/IM5 mypy seam) --------------------
    g.add_node(_TRIAGE, triage)
    g.add_node(_PRODUCT, product_node)
    g.add_node(_ARCHITECTURE, architecture_node)
    g.add_node(_SPEC_GATE, spec_gate)
    g.add_node(_IMPLEMENTATION, implementation_node)
    g.add_node(_QUALITY, quality_node)
    g.add_node(_BUILD_TEST_GATE, build_test_gate)
    g.add_node(_SPEC_FIX, spec_fix)
    g.add_node(_BUILD_TEST_FIX, build_test_fix)

    # -- wire the phase sequence ----------------------------------------------
    g.add_edge(START, _TRIAGE)
    g.add_edge(_TRIAGE, _PRODUCT)
    g.add_edge(_PRODUCT, _ARCHITECTURE)

    if gated:
        # Design sign-off boundary AFTER architecture, BEFORE the spec gate
        # (the integrity boundary is unchanged — the sign-off is an ADDED
        # boundary, never a relaxation).
        g.add_node(_DESIGN_SIGN_OFF, design_sign_off)
        g.add_node(_ASSURANCE, assurance_node)
        g.add_node(_RELEASE_SIGN_OFF, release_sign_off)
        g.add_edge(_ARCHITECTURE, _DESIGN_SIGN_OFF)
        g.add_edge(_DESIGN_SIGN_OFF, _SPEC_GATE)
    else:
        g.add_edge(_ARCHITECTURE, _SPEC_GATE)

    # SPEC boundary: advance -> implementation; fix -> back to architecture;
    # halt -> END. The path map lists every legal router return value.
    g.add_conditional_edges(
        _SPEC_GATE,
        _route_spec,
        {"advance": _IMPLEMENTATION, "fix": _SPEC_FIX, "halt": END},
    )
    g.add_edge(_SPEC_FIX, _ARCHITECTURE)  # re-verify the responsible layer

    g.add_edge(_IMPLEMENTATION, _QUALITY)
    g.add_edge(_QUALITY, _BUILD_TEST_GATE)

    if gated:
        # BUILD+TEST boundary: advance -> the assurance subgraph; fix -> back to
        # implementation; halt -> END. Then assurance advance -> release sign-off
        # (release_allowed-gated emit), assurance halt -> END (a vulnerable
        # change caught -> NO release path).
        g.add_conditional_edges(
            _BUILD_TEST_GATE,
            _route_build_test,
            {"advance": _ASSURANCE, "fix": _BUILD_TEST_FIX, "halt": END},
        )
        g.add_conditional_edges(
            _ASSURANCE,
            _route_assurance,
            {ASSURANCE_ADVANCE: _RELEASE_SIGN_OFF, ASSURANCE_HALT: END},
        )
        # Release boundary: release_allowed -> END (emit); blocked -> END
        # (fail-closed). Both terminate the supervisor; factory_graph appends the
        # emit node AFTER END only on a release-admitted run.
        g.add_conditional_edges(
            _RELEASE_SIGN_OFF,
            _route_release,
            {ASSURANCE_ADVANCE: END, ASSURANCE_HALT: END},
        )
    else:
        # BUILD+TEST boundary: advance -> END; fix -> back to implementation;
        # halt -> END.
        g.add_conditional_edges(
            _BUILD_TEST_GATE,
            _route_build_test,
            {"advance": END, "fix": _BUILD_TEST_FIX, "halt": END},
        )
    g.add_edge(_BUILD_TEST_FIX, _IMPLEMENTATION)  # re-verify the responsible layer

    return g
