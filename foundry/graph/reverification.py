r"""Dependency-scoped re-verification — re-verification is change-proportional.

(governance §1f, architecture.md §4, implementation-plan §3.1)

A code change need NOT re-run every integrated gate from scratch. This engine
derives, from the delta between the prior verified tree and the new one, **which
gates the change can affect** and re-runs only those; verdicts for unaffected
inputs are **REUSED from the prior attested verdict, keyed on the unchanged
input sub-hash** (governance §1f). A cached PASS keyed on an unchanged
sub-tree / lockfile / scanner-config hash is still a *real* runner-attested
verdict, so the final attested gate-state still covers the whole tree.

Two pure steps, decoupled from any I/O:

* :func:`changed_scope` — compute the CHANGE SET between two states: which files
  changed (added / removed / modified), and whether the lockfile / toolchain /
  scanner-config (the cross-cutting dependency inputs that live in / bind the
  ``tree_sha``) moved. It takes the materialized file maps + the dependency revs
  for the old and new state; the caller produces those from the content-addressed
  handles (the SAME ``materialize_workdir`` seam the gate wiring uses).
* :func:`reverification_plan` — for each gate, decide RE-RUN vs REUSE.

The decision (the whole point — SOUND + change-proportional):

* **REUSE** a gate's verdict iff a prior attested verdict exists whose
  ``reuse_key`` (``runner.reuse_key(gate, new_state_sha)``) EXACTLY matches the
  reuse key the prior verdict was recorded under AND the change set does not
  intersect the gate's dependency scope. Because the reuse key for a
  config-evaluable gate is the ``state_sha`` itself, and for an
  execution-dependent (class-2) gate additionally binds ``mutant_rotation_id +
  scanner_rule_version`` (gov §1c/§1f), ANY change to the source sub-hash, the
  lockfile, the toolchain, the scanner config, or a mutant rotation re-keys the
  gate and forbids reuse. We reuse ONLY on an exact key match — never a "close
  enough" tree.

* **RE-RUN** otherwise: the gate's inputs changed (its scope intersects the
  change set, or its reuse key differs from the prior verdict's), OR there is no
  prior attested verdict to reuse. This is FAIL-CLOSED — when in doubt, re-run.

* **Change-PROPORTIONAL.** A gate's scope is the set of inputs it actually reads
  (Spec → ``criteria.json``; Build/Test/E2E → the code + test sources; Security
  → all sources; Audit/Supply-chain → the lockfile + the diff). A one-file
  change re-runs only the gates whose scope includes that file (+ the
  cross-cutting dependency gates iff a dependency moved), NOT the whole gate set.
  A cosmetic change to an unrelated file does NOT force a full re-verify.

SOUNDNESS invariant (the load-bearing property): it is IMPOSSIBLE to REUSE a
verdict whose inputs changed. Reuse requires (a) the gate's scope is disjoint
from the change set AND (b) an exact reuse-key match against a still-valid prior
attested verdict. A missing / mismatched prior verdict → RE-RUN. There is no
path that carries a stale verdict forward.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from foundry.canonical import content_digest
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateResult, GateStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from foundry.gates.runner import GateImpl, TwoZoneGateRunner

__all__ = [
    "DEFAULT_GATE_SCOPES",
    "ChangedScope",
    "GateDecision",
    "GateScope",
    "ReverificationPlan",
    "changed_scope",
    "reverification_plan",
]


# ---- gate dependency scopes -------------------------------------------------
#
# A gate's SCOPE is the class of inputs it actually reads, so a change that does
# not touch a gate's scope cannot change its verdict. Two orthogonal facets:
#
#   * ``reads_sources`` — the gate runs over (a subset of) the materialized
#     source/test files. A changed FILE in that subset forces a re-run.
#   * ``reads_lockfile`` / ``reads_toolchain`` / ``reads_scanner_config`` — the
#     gate depends on a cross-cutting dependency input bound into the tree_sha.
#     A move of that input forces a re-run regardless of which files changed.
#
# A predicate ``source_selector`` narrows ``reads_sources`` to the exact subset
# of changed paths the gate cares about (e.g. Spec only cares about
# ``criteria.json``), so the change-proportional re-run set is as tight as the
# gate's real dependency footprint.


def _is_lockfile(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base in {"uv.lock", "poetry.lock", "Pipfile.lock"} or (
        base.startswith("requirements") and base.endswith(".txt")
    )


def _is_criteria(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "criteria.json"


def _is_diff(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "changes.diff"


# Files that are gate-specific GROUNDING inputs, not generic "source code": the
# criteria document, the integrated diff text, and the dependency lockfiles. A
# gate with the default (whole-tree) source footprint reads CODE/TEST files, not
# these — they are policed by the criteria-only / lockfile / diff scopes. So a
# pure lockfile bump does NOT count as a "source change" for the compile gate.
def _is_generic_source(path: str) -> bool:
    return not (_is_criteria(path) or _is_diff(path) or _is_lockfile(path))


class GateScope(FrozenModel):
    """The dependency footprint of one gate — what a change must touch to re-run it.

    The gate's source-subset selector (resolved by gate name from
    ``_SOURCE_SELECTORS``) is a predicate over a changed relative-POSIX path: it
    returns True iff that file is in the gate's source footprint. The DEFAULT
    (gates not in the table, e.g. Build / Test / Security) reads every GENERIC
    source file — code + tests, but NOT the gate-specific grounding inputs
    (criteria.json / changes.diff / lockfiles), which are policed by the
    criteria-only / diff / lockfile scopes. The three cross-cutting booleans bind
    the gate to the lockfile / toolchain / scanner config (the dependency inputs
    that move the ``tree_sha`` / reuse key).
    """

    gate_name: str
    reads_sources: bool = True
    reads_lockfile: bool = False
    reads_toolchain: bool = True
    reads_scanner_config: bool = False
    # NB: a predicate is not a pydantic-serializable field; we keep it OUT of the
    # frozen model and resolve it from ``DEFAULT_SOURCE_SELECTORS`` by gate name.


# Per-gate source-subset selectors. Absent (None) => the gate reads ALL sources.
# Keyed by gate name so the frozen GateScope stays serializable.
_SOURCE_SELECTORS: dict[str, object] = {
    "spec": _is_criteria,  # Spec only reads criteria.json
    "audit": _is_lockfile,  # Audit's inventory is the lockfile
    "supply_chain": _is_diff,  # Supply-chain polices the integrated diff text
}


# The default scope table for the M1 + M2 gate families. Build / Test / E2E run
# over the code+test sources; Security scans every source; Spec is criteria-only;
# Audit + Supply-chain are the dependency-policy gates (lockfile / diff).
DEFAULT_GATE_SCOPES: dict[str, GateScope] = {
    "spec": GateScope(gate_name="spec", reads_scanner_config=False),
    "build": GateScope(gate_name="build"),
    "test": GateScope(gate_name="test", reads_scanner_config=True),
    "e2e": GateScope(gate_name="e2e", reads_scanner_config=True),
    "security": GateScope(
        gate_name="security", reads_lockfile=True, reads_scanner_config=True
    ),
    "audit": GateScope(gate_name="audit", reads_lockfile=True),
    "supply_chain": GateScope(gate_name="supply_chain", reads_lockfile=True),
}


# ---- the change set ---------------------------------------------------------


class ChangedScope(FrozenModel):
    """The dependency-scoped delta between the prior verified tree and the new one.

    Pure data (a frozen, hashable record): WHICH files changed and whether the
    cross-cutting dependency inputs (lockfile / toolchain / scanner config)
    moved. The reverification planner reads it; it does no I/O itself.

    ``changed_files`` is the union of added, removed, and content-modified
    relative-POSIX paths. A file present-and-identical in both states is NOT in
    it (a cosmetic touch that does not change content is not a change — content,
    never mtime, drives ``tree_sha``, DS4). ``old_tree_sha`` / ``new_tree_sha``
    are the two integrated digests (informational; the planner keys on the
    runner reuse key, not the raw tree sha).
    """

    changed_files: frozenset[str] = frozenset()
    added_files: frozenset[str] = frozenset()
    removed_files: frozenset[str] = frozenset()
    lockfile_changed: bool = False
    toolchain_changed: bool = False
    scanner_config_changed: bool = False
    old_state_sha: str | None = None
    new_state_sha: str | None = None

    @property
    def any_change(self) -> bool:
        """True iff anything moved at all (files or a cross-cutting dependency)."""
        return bool(
            self.changed_files
            or self.lockfile_changed
            or self.toolchain_changed
            or self.scanner_config_changed
        )


def changed_scope(
    old_files: Mapping[str, str],
    new_files: Mapping[str, str],
    *,
    old_state_sha: str | None = None,
    new_state_sha: str | None = None,
    old_toolchain_digest: str | None = None,
    new_toolchain_digest: str | None = None,
    old_scanner_config_rev: str | None = None,
    new_scanner_config_rev: str | None = None,
) -> ChangedScope:
    """Compute the dependency-scoped change set between two materialized trees.

    ``old_files`` / ``new_files`` are relative-POSIX-path -> file-content maps —
    exactly what the caller materializes from the content-addressed handles on
    each state (the SAME materialization the gate wiring runs the gates over). A
    file is CHANGED iff it is added, removed, or its content differs; a file
    present-and-byte-identical in both is NOT a change (content drives tree_sha,
    not mtime — DS4 — so an identical re-emission is not a re-verify trigger).

    ``lockfile_changed`` is derived from a content change to ANY recognised
    lockfile path (``uv.lock`` / ``requirements*.txt`` / ``poetry.lock`` /
    ``Pipfile.lock``). ``toolchain_changed`` / ``scanner_config_changed`` come
    from the explicit old/new dependency revs (a toolchain or scanner-rule bump
    is not visible in the file set; the caller passes the pinned revs). Each is
    fail-open toward RE-RUN: an unknown (``None``) old rev compared to a present
    new rev counts as changed.
    """
    old_keys = set(old_files)
    new_keys = set(new_files)
    added = new_keys - old_keys
    removed = old_keys - new_keys
    modified = {p for p in old_keys & new_keys if old_files[p] != new_files[p]}
    changed = added | removed | modified

    lockfile_changed = any(_is_lockfile(p) for p in changed)
    toolchain_changed = old_toolchain_digest != new_toolchain_digest
    scanner_config_changed = old_scanner_config_rev != new_scanner_config_rev

    return ChangedScope(
        changed_files=frozenset(changed),
        added_files=frozenset(added),
        removed_files=frozenset(removed),
        lockfile_changed=lockfile_changed,
        toolchain_changed=toolchain_changed,
        scanner_config_changed=scanner_config_changed,
        old_state_sha=old_state_sha,
        new_state_sha=new_state_sha,
    )


# ---- the plan ---------------------------------------------------------------


class GateDecision(FrozenModel):
    """The per-gate re-verification decision: RE-RUN or REUSE, with the reason.

    ``rerun`` is the verdict bit. On REUSE, ``reused_verdict`` carries the prior
    attested :class:`GateResult` to fold forward verbatim and ``reuse_key`` is
    the exact key it matched on. On RE-RUN, ``reused_verdict`` is ``None`` and
    ``reason`` names why (scope intersection / reuse-key mismatch / no prior
    verdict — fail-closed).
    """

    gate_name: str
    rerun: bool
    reason: str
    reuse_key: str | None = None
    reused_verdict: GateResult | None = None


class ReverificationPlan(FrozenModel):
    """The whole-boundary plan: one :class:`GateDecision` per gate.

    The union of ``rerun`` gates' fresh verdicts + ``reuse`` gates' cached
    attested verdicts is the integrated gate-state covering the WHOLE tree (gov
    §1f): a cached PASS keyed on an unchanged sub-hash is still a real
    runner-attested verdict.
    """

    decisions: tuple[GateDecision, ...]

    @property
    def rerun_gates(self) -> tuple[str, ...]:
        return tuple(d.gate_name for d in self.decisions if d.rerun)

    @property
    def reuse_gates(self) -> tuple[str, ...]:
        return tuple(d.gate_name for d in self.decisions if not d.rerun)

    @property
    def reused_verdicts(self) -> tuple[GateResult, ...]:
        return tuple(
            d.reused_verdict for d in self.decisions if d.reused_verdict is not None
        )

    def decision_for(self, gate_name: str) -> GateDecision | None:
        for d in self.decisions:
            if d.gate_name == gate_name:
                return d
        return None


def _scope_intersects_change(
    scope: GateScope, scope_name: str, changed: ChangedScope
) -> bool:
    """True iff the change set touches anything this gate's scope reads.

    A gate re-runs if a cross-cutting dependency it reads moved (lockfile /
    toolchain / scanner config) OR a source file in its footprint changed. The
    source footprint is narrowed by the gate's ``source_selector`` (None = whole
    tree) so the re-run set is change-proportional.
    """
    if scope.reads_lockfile and changed.lockfile_changed:
        return True
    if scope.reads_toolchain and changed.toolchain_changed:
        return True
    if scope.reads_scanner_config and changed.scanner_config_changed:
        return True
    if scope.reads_sources and changed.changed_files:
        selector = _SOURCE_SELECTORS.get(scope_name, _is_generic_source)
        return any(selector(p) for p in changed.changed_files)  # type: ignore[operator]
    return False


def _prior_for(gate_name: str, prior_verdicts: Iterable[GateResult]) -> GateResult | None:
    """The newest-by-Seq prior attested verdict for ``gate_name``, if any.

    ``prior_verdicts`` is the already-trust-filtered prior ``gates`` channel
    (only runner-attested records survived ``merge_gates``). A reusable verdict
    must be a real, non-ERROR attested verdict — an ERROR is infra, not a
    verdict (DS7), so it is never carried forward as a "still valid" result.
    """
    best: GateResult | None = None
    for v in prior_verdicts:
        if v.name != gate_name:
            continue
        if v.status is GateStatus.ERROR:
            continue  # infra failure is not a reusable verdict
        if best is None or v.seq > best.seq:
            best = v
    return best


def _scoped_reuse_key(
    gate: GateImpl,
    runner: TwoZoneGateRunner,
    new_state_sha: str,
    *,
    in_scope: bool,
    scanner_config_rev: str,
) -> str:
    """The gate's dependency-SCOPED reuse key (the §1f "unchanged input sub-hash").

    Folds the runner's own reuse key (which binds the class-2 anti-gaming values —
    ``mutant_rotation_id`` + ``scanner_rule_version`` — for execution-dependent
    gates, else the state_sha) with (a) whether THIS gate's scoped inputs moved
    and (b) the current scanner config rev. Two states whose gate sub-scope is
    unchanged share this key; any scoped change perturbs it. Informational — the
    rerun/reuse decision is driven by the same components directly.
    """
    return content_digest(
        {
            "kind": "scoped_reuse_key",
            "gate": gate.name,
            "runner_key": runner.reuse_key(gate, new_state_sha),
            "scope_changed": in_scope,
            "scanner_config_rev": scanner_config_rev,
        }
    )


def reverification_plan(
    prior_verdicts: Iterable[GateResult],
    changed: ChangedScope,
    *,
    gates: Iterable[GateImpl],
    runner: TwoZoneGateRunner,
    new_state_sha: str,
    scanner_config_rev: str,
    scopes: Mapping[str, GateScope] | None = None,
) -> ReverificationPlan:
    """Decide RE-RUN vs REUSE per gate — change-proportional + sound (gov §1f).

    For each gate in ``gates`` we build the gate's DEPENDENCY-SCOPED reuse key
    (the §1f "unchanged input sub-hash") and decide:

    * The reuse key binds ONLY the inputs this gate actually reads (its scope):
      whether its scoped source footprint / lockfile / toolchain / scanner config
      changed, plus — for a class-2 (execution-dependent) gate — the runner's
      anti-gaming binding via ``runner.reuse_key`` (``mutant_rotation_id +
      scanner_rule_version``, gov §1c/§1f). It is NOT the whole-tree ``tree_sha``:
      keying a criteria-only gate (Spec) on the whole tree would re-run it on
      every unrelated code edit, defeating change-proportionality. Keying on the
      gate's sub-scope is exactly the §1f "unchanged input sub-hash".
    * REUSE iff (a) the gate's dependency scope is DISJOINT from the change set
      (its scoped sub-hash is unchanged) AND (b) there is a prior attested
      non-ERROR verdict whose recorded ``scanner_config_rev`` matches the current
      one (a scanner-config boundary forbids reuse — no stale class-2 verdict is
      laundered forward). Carry that verdict forward verbatim.
    * RE-RUN otherwise — a scoped input changed, the scanner config moved, or
      there is no prior verdict to reuse. FAIL-CLOSED: when in doubt, re-run.

    The scope check (a) makes the plan change-PROPORTIONAL: a one-file change
    re-runs only the gates whose scope includes that file (+ the dependency gates
    iff a dependency moved), not the whole gate set. The reuse condition is an
    EXACT match on the gate's RELEVANT inputs (sub-scope unchanged + scanner
    config unchanged + a valid attested prior), so it is impossible to reuse a
    verdict whose inputs changed.

    ``prior_verdicts`` MUST be the trust-filtered prior ``gates`` channel (only
    runner-attested verdicts survived ``merge_gates``); we never reuse an
    unsigned record. ``new_state_sha`` is the integrated digest of the new tree
    (informational, recorded on each decision's ``reuse_key``). ``scanner_config_rev``
    is the CURRENT runner's scanner config revision: a prior verdict recorded
    under a different ``scanner_config_rev`` is NOT reusable (gov §1c/§1f).
    ``scopes`` defaults to :data:`DEFAULT_GATE_SCOPES`.
    """
    table = dict(DEFAULT_GATE_SCOPES if scopes is None else scopes)
    priors = list(prior_verdicts)
    decisions: list[GateDecision] = []

    for gate in gates:
        name = gate.name
        scope = table.get(name) or GateScope(gate_name=name)

        # (a) Is the change set within this gate's dependency scope?
        in_scope = _scope_intersects_change(scope, name, changed)

        # The gate's dependency-SCOPED reuse key (the §1f sub-hash): the runner's
        # reuse key (binds the class-2 anti-gaming values for execution gates,
        # else the state_sha) folded with whether THIS gate's scoped inputs moved
        # + the current scanner config. Two states with the gate's sub-scope
        # unchanged share this key; any scoped change perturbs it.
        new_key = _scoped_reuse_key(
            gate, runner, new_state_sha, in_scope=in_scope, scanner_config_rev=scanner_config_rev
        )

        # (b) Is there a prior attested non-ERROR verdict still valid under the
        #     current scanner config? For a SCANNER-BOUND gate, a verdict recorded
        #     under a DIFFERENT scanner config is not reusable — a rule-version
        #     boundary can never launder a stale (class-2) verdict forward (gov
        #     §1c/§1f). A gate that does not read the scanner config is unaffected
        #     by a scanner bump (change-proportional).
        prior = _prior_for(name, priors)
        scanner_ok = not scope.reads_scanner_config or (
            prior is not None and prior.scanner_config_rev == scanner_config_rev
        )
        key_matches = prior is not None and scanner_ok and not in_scope

        if not in_scope and key_matches and prior is not None:
            decisions.append(
                GateDecision(
                    gate_name=name,
                    rerun=False,
                    reason="reuse: scope disjoint from change + exact reuse-key match",
                    reuse_key=new_key,
                    reused_verdict=prior,
                )
            )
            continue

        if prior is None:
            reason = "rerun: no prior attested verdict (fail-closed)"
        elif in_scope:
            reason = "rerun: change set intersects gate scope"
        elif not scanner_ok:
            reason = "rerun: scanner-config boundary (prior verdict re-keyed)"
        else:
            reason = "rerun: reuse-key mismatch (inputs changed)"
        decisions.append(
            GateDecision(gate_name=name, rerun=True, reason=reason, reuse_key=new_key)
        )

    return ReverificationPlan(decisions=tuple(decisions))
