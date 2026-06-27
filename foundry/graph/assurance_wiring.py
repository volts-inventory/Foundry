r"""Graph-side ASSURANCE gate wiring — the trust seam at the assurance boundary.

(governance §1/§1e, architecture.md §3/§4, implementation-plan §5.1)

This is the SIBLING of :mod:`foundry.graph.gate_wiring` for the Phase-2 (Gated
mode) assurance boundary. Where ``gate_wiring`` runs the Spec / Build / Test
gates at the product / implementation boundaries, THIS module runs the
Security / Audit / Supply-chain / E2E gates at the assurance boundary — through
the SAME :class:`~foundry.gates.runner.TwoZoneGateRunner`, folding ONLY the
runner-ATTESTED verdicts onto the ``gates`` channel.

It REUSES ``gate_wiring``'s materializer + routing scaffolding verbatim (it does
NOT re-implement or edit them):

* :func:`gate_wiring.materialize_workdir` writes the integrated tree (criteria +
  code + test sources) from the content-addressed handles on ``state`` and
  returns its ``tree_sha``. The Security gate scans those sources; the E2E gate
  runs the test sources.
* The Audit + Supply-chain gates need TWO further grounding inputs the base
  materializer does not write — a LOCKFILE inventory and the raw unified-DIFF
  text (``changes.diff``). :func:`materialize_assurance_workdir` materializes the
  base tree, then ADDITIONALLY writes the concatenated diff bodies as
  ``changes.diff`` so the Supply-chain gate can police the build/CI/IaC/dep edits
  off the genuine diff text — and RE-HASHES the augmented tree so the integrated
  ``tree_sha`` the verdicts bind / latch against reflects the EXACT tree the
  gates ran over.

Invariants (the assurance subgraph + supervisor reuse these):

* **Verdicts come ONLY from the runner (LEARNINGS L5).** Nothing here ever
  constructs a :class:`GateResult`; the runner executes zone A, decides + signs
  in zone B, returns an attested record. ``merge_gates`` admits a
  deterministic-gate write iff it carries a valid zone-B attestation, so an
  UNSIGNED / agent ``GateResult`` is dropped fail-closed and can NEVER gate
  advancement. We only ever forward what the runner handed back.

* **The tier scales DEPTH, never INTEGRITY (governance §1e).** The integrity
  FLOOR — Security + Audit — ALWAYS runs, fail-closed, at every tier (a LOW tier
  never skips the security floor). The tier only ADDS depth: Supply-chain at
  STANDARD+ and E2E at HIGH. ``TierDecision.relaxes_integrity()`` is always
  False by design; this module never reads a tier to DROP a floor gate, only to
  ADD optional ones.

* **Routing is fail-closed + convergence-bounded.** :func:`route_after_assurance`
  mirrors ``gate_wiring.route_after_gates``: a stable-FAIL latch on the
  integrated digest → ``halt`` (terminal, cross-fork); a FAIL/ERROR (or a missing
  relevant verdict) → bounded ``fix``, then ``halt`` once the bound is spent;
  all-PASS → ``advance``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.gates.audit import AuditGate
from foundry.gates.e2e import E2EGate
from foundry.gates.security import SecurityGate
from foundry.gates.supply_chain import SupplyChainGate
from foundry.graph import gate_wiring
from foundry.graph.gate_wiring import (
    FEATURE_DIFF_KIND,
    materialize_workdir,
)
from foundry.schemas.gates import GateStatus
from foundry.schemas.governance import RiskTier, TierDecision
from foundry.schemas.implementation import FeatureDiff
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.gates.runner import GateImpl, TwoZoneGateRunner
    from foundry.state.factory_state import FactoryState

    # The SAME two seams the base materializer uses (gate_wiring, D-R1-01/02);
    # both are TYPE_CHECKING-only there, so we mirror the aliases locally rather
    # than import them at runtime. ``resolve`` reconstitutes a TYPED artifact from
    # its handle; ``resolve_blob`` fetches the raw diff BODY keyed on its uri.
    ArtifactResolver = gate_wiring.ArtifactResolver
    BlobResolver = gate_wiring.BlobResolver

__all__ = [
    "ASSURANCE_BOUNDARY",
    "DIFF_FILENAME",
    "assurance_gate_set",
    "materialize_assurance_workdir",
    "route_after_assurance",
    "run_assurance_gates",
]

# The single boundary this module is responsible for.
ASSURANCE_BOUNDARY = "assurance"

# The integrated unified-diff text the Supply-chain gate polices (its default
# ``diff_filename``). Written by :func:`materialize_assurance_workdir` alongside
# the base tree so the gate has its grounding input.
DIFF_FILENAME = "changes.diff"

# Reuse the base materializer's toolchain digest so the augmented tree's
# ``tree_sha`` stays a pure function of the materialized source (DS4).
_TOOLCHAIN_DIGEST = gate_wiring._TOOLCHAIN_DIGEST

# Distinct node ids so each verdict gets an independent resume-monotonic epoch
# stream (AR2), keyed on the gate's grounding.
_SECURITY_NODE = "security_gate"
_AUDIT_NODE = "audit_gate"
_SUPPLY_CHAIN_NODE = "supply_chain_gate"
_E2E_NODE = "e2e_gate"

# Integrity FLOOR — runs at EVERY tier, fail-closed (gov §1e: tiering scales
# depth, never integrity, so LOW never skips these).
_FLOOR_GATES = ("security", "audit")
# Optional DEPTH the tier ADDS on top of the floor (never instead of it).
_DEPTH_GATES = ("supply_chain", "e2e")


def assurance_gate_set(tier: TierDecision | None) -> tuple[str, ...]:
    """The assurance gate family this tier runs — floor + tier-scaled depth.

    The integrity FLOOR (Security + Audit) is ALWAYS present regardless of tier
    (gov §1e — a LOW tier never skips the security floor). The tier only ADDS
    optional depth:

    * ``None`` / LOW  → floor only (Security + Audit).
    * STANDARD        → + Supply-chain (build/CI/IaC/dep policy over the diff).
    * HIGH            → + Supply-chain + E2E (full end-to-end depth).

    Returned as an ordered tuple so the materialized verdicts (and thus the fold)
    are deterministic. This is depth-ONLY: no path through here ever removes a
    floor gate, so :meth:`TierDecision.relaxes_integrity` stays vacuously False.
    """
    gates: list[str] = list(_FLOOR_GATES)
    tier_level = tier.tier if tier is not None else RiskTier.LOW
    if tier_level in (RiskTier.STANDARD, RiskTier.HIGH):
        gates.append("supply_chain")
    if tier_level is RiskTier.HIGH:
        gates.append("e2e")
    return tuple(gates)


# ---- materialization: base tree + the diff text the assurance gates need ----


def _integrated_diff_text(
    state: FactoryState, *, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> str:
    """The concatenated unified-DIFF text the Supply-chain gate polices.

    Reuses the SAME two seams the base materializer uses (D-R1-01/02): the typed
    ``resolve`` reconstitutes each :class:`FeatureDiff` from its handle, and the
    SEPARATE ``resolve_blob`` fetches the raw unified-diff BODY keyed on the
    ``cas://`` ``diff_uri`` (prod: ``sandbox.get_blob``). Diffs are concatenated
    in ``Seq`` order (later/retry last, matching the base materializer's
    write order, L4) so the policed text is deterministic across folds.

    Fail-closed: a diff body that is neither ``bytes`` nor ``str`` raises a
    :class:`gate_wiring.FeatureDiffBodyError` (the base materializer would too).
    """
    artifacts = [a for a in (state.get("artifacts") or ()) if a.kind == FEATURE_DIFF_KIND]
    parts: list[str] = []
    for artifact in sorted(artifacts, key=lambda a: a.seq):
        diff = resolve(artifact)
        if not isinstance(diff, FeatureDiff):
            raise gate_wiring.MaterializationError(
                f"resolved {FEATURE_DIFF_KIND!r} handle {artifact.uri!r} to "
                f"{type(diff).__name__}, expected FeatureDiff"
            )
        body = resolve_blob(diff.diff_uri)
        if isinstance(body, bytes):
            parts.append(body.decode("utf-8"))
        elif isinstance(body, str):
            parts.append(body)
        else:
            raise gate_wiring.FeatureDiffBodyError(
                f"FeatureDiff body for {diff.diff_uri!r} resolved to "
                f"{type(body).__name__}, expected the unified-diff blob (bytes|str)"
            )
    return "".join(parts)


def materialize_assurance_workdir(
    state: FactoryState,
    dest: Path,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> str:
    """Materialize the integrated tree for the assurance gates; return its tree_sha.

    Builds on :func:`gate_wiring.materialize_workdir` (the SAME trust-seam
    contract the M1 boundaries use — reused verbatim, NOT re-implemented): it
    writes ``criteria.json`` + the integrated code/test sources from the
    content-addressed handles on ``state``, fail-closed.

    The Audit + Supply-chain gates need TWO further grounding inputs the base
    materializer does not write:

    * ``changes.diff`` — the concatenated unified-DIFF text the Supply-chain gate
      polices (its default ``diff_filename``). The Audit gate's lockfile inventory
      rides in via the FeatureDiff sources the base materializer already wrote
      (an integrated change that touches ``uv.lock`` / ``requirements*.txt`` lands
      as a file in the tree).

    Because the augmented tree differs from the base tree, we RE-HASH it: the
    returned ``tree_sha`` is the digest of the EXACT tree the assurance gates run
    over (DS4) — the integrated ``run_against`` the verdicts bind + (on FAIL)
    latch against.
    """
    # Base tree (criteria + sources), fail-closed; the returned digest is
    # superseded by the post-augmentation re-hash below.
    materialize_workdir(state, dest, resolve=resolve, resolve_blob=resolve_blob)
    diff_text = _integrated_diff_text(state, resolve=resolve, resolve_blob=resolve_blob)
    (dest / DIFF_FILENAME).write_text(diff_text, encoding="utf-8")
    # prefer_git=False: dest is a fresh materialized tree, not a git worktree.
    return tree_sha(dest, toolchain_digest=_TOOLCHAIN_DIGEST, prefer_git=False)


# ---- gate construction (tier-scaled) ----------------------------------------


def _assurance_gates(
    tier: TierDecision | None,
    *,
    allow_build_changes: bool,
) -> list[tuple[str, GateImpl]]:
    """The (node_id, gate) pairs to run for this tier — floor + scaled depth.

    Mirrors :func:`assurance_gate_set`: Security + Audit ALWAYS (the integrity
    floor); Supply-chain at STANDARD+; E2E at HIGH. The Security gate runs its
    pure-python secret scan — real machine grounding, no external binary.
    """
    selected = assurance_gate_set(tier)
    builders: dict[str, tuple[str, GateImpl]] = {
        "security": (_SECURITY_NODE, SecurityGate()),
        "audit": (_AUDIT_NODE, AuditGate()),
        "supply_chain": (
            _SUPPLY_CHAIN_NODE,
            SupplyChainGate(diff_filename=DIFF_FILENAME, allow_build_changes=allow_build_changes),
        ),
        "e2e": (_E2E_NODE, E2EGate()),
    }
    return [builders[name] for name in selected]


def run_assurance_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    tier: TierDecision | None = None,
    allow_build_changes: bool = False,
) -> dict[str, Any]:
    """Run the assurance gates at the assurance boundary; return attested verdicts.

    Materializes the integrated tree ONCE (criteria + sources + ``changes.diff``)
    into ``workdir`` and runs the tier-scaled assurance gate family against its
    ``tree_sha`` (so every verdict binds / latches to ONE integrated state). Each
    gate runs through ``runner.run(...)`` — the runner attests; this helper NEVER
    constructs or signs a :class:`GateResult`, it only forwards the attested
    records (L5). Returns ``{"gates": [<attested>, ...]}``.

    The tier scales DEPTH, never INTEGRITY (gov §1e): Security + Audit always run
    (the floor — a LOW tier never skips them); Supply-chain is added at STANDARD+,
    E2E at HIGH. The real ``merge_gates`` admits each verdict iff attested, so an
    unsigned / agent record can never reach the channel or gate advancement.
    """
    run_against = materialize_assurance_workdir(
        state, workdir, resolve=resolve, resolve_blob=resolve_blob
    )
    results = []
    for node_id, gate in _assurance_gates(tier, allow_build_changes=allow_build_changes):
        seq = rt.seq(config, node_id, f"verdict:{gate.name}")
        results.append(runner.run(gate, run_against, seq, workdir))
    return {"gates": results}


# ---- routing predicate (fail-closed, convergence-bounded) -------------------

ADVANCE = "advance"  # all relevant assurance gates PASS — proceed past the boundary
FIX = "fix"  # a FAIL/ERROR within the convergence bound — back to the layer
HALT = "halt"  # bound exhausted or a stable-FAIL latch — escalate / stop

# Every gate name the assurance boundary can be responsible for (the HIGH-tier
# superset; lower tiers simply produce fewer verdicts, never a different family).
_ASSURANCE_GATES = frozenset({*_FLOOR_GATES, *_DEPTH_GATES})


def _relevant_latest(
    state: FactoryState, gate_names: frozenset[str]
) -> dict[str, Any]:
    """Newest-by-`Seq` verdict per relevant assurance gate on ``state['gates']``.

    The channel is already trust-filtered (``merge_gates`` admitted only
    runner-attested records, L5) and keyed by ``(name, run_against)``; pick the
    latest by ``Seq`` per name so a re-run's verdict (bumped epoch) wins.
    """
    latest: dict[str, Any] = {}
    for g in state.get("gates") or ():
        if g.name not in gate_names:
            continue
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def route_after_assurance(
    state: FactoryState,
    config: RunnableConfig,
    *,
    records: Any,
    tier: TierDecision | None = None,
    max_fix_attempts: int = 3,
) -> str:
    """Route the assurance boundary on its relevant verdicts. Fail-closed.

    Mirrors :func:`gate_wiring.route_after_gates`:

    * :data:`HALT` — a ``records.stable_fail_latched(run_against, name)`` on ANY
      relevant verdict (terminal, cross-fork, monotonic — a later PASS never
      auto-clears it; routing reads the latch REGARDLESS of channel content), OR
      the ``max_fix_attempts`` convergence bound exhausted.
    * :data:`FIX` — a FAIL or ERROR (infra OR flake), or a relevant verdict
      MISSING entirely (defense-in-depth: never advance on an absent verdict),
      within the convergence bound; route back to the responsible layer.
    * :data:`ADVANCE` — every relevant assurance gate has a PASS verdict and
      nothing is latched.

    ``records`` is the cross-fork :class:`GlobalRecordsView` consulted for the
    stable-FAIL latch (a mutable committed read, never cached). ``tier`` scopes
    WHICH verdicts must be present to advance — the SAME tier-scaled set
    :func:`run_assurance_gates` produced (floor + scaled depth), so a LOW tier
    advances on Security+Audit PASS while a HIGH tier additionally requires
    Supply-chain + E2E PASS. An UNSIGNED verdict never reaches this point — it was
    dropped by ``merge_gates`` before the channel was folded (L5).
    """
    gate_names = frozenset(assurance_gate_set(tier))
    latest = _relevant_latest(state, gate_names)

    # 1. Stable-FAIL latch is terminal — keyed on the integrated digest the
    #    verdict ran against (monotonic, cross-fork; gov §5). Read REGARDLESS of
    #    channel content so a status-flipped copy can't sneak past.
    for g in latest.values():
        if records.stable_fail_latched(g.run_against, g.name):
            return HALT

    bound_spent = _fix_attempts(state, config) >= max_fix_attempts

    # 2. A FAIL/ERROR, or a MISSING relevant verdict, needs a fix — or HALT once
    #    the convergence bound is exhausted (fail-closed: never advance).
    needs_fix = False
    for name in gate_names:
        verdict = latest.get(name)
        if verdict is None:
            needs_fix = True  # missing relevant verdict: never advance (fail-closed)
            continue
        if verdict.status in (GateStatus.FAIL, GateStatus.ERROR):
            needs_fix = True

    if needs_fix:
        return HALT if bound_spent else FIX

    # 3. All relevant assurance gates PASS and nothing is latched.
    return ADVANCE


def _fix_attempts(state: FactoryState, config: RunnableConfig) -> int:
    """The convergence bound's spent attempts (mirrors gate_wiring._fix_attempts).

    Read from the budget ledger's ``fix_attempts_since_all_green`` if present,
    else from the run config — the supervisor owns the counter; the routing
    predicate only READS it (fold-order independent).
    """
    budget = state.get("budget")
    if budget is not None:
        return int(budget.fix_attempts_since_all_green)
    configurable = config.get("configurable") or {}
    return int(configurable.get("fix_attempts", 0))
