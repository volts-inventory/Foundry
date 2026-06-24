"""The evolution route — ``evolve <existing repo> "<change>"``. (lifecycle.md §1/§3)

A change request against an ALREADY-RELEASED codebase is NOT a fresh build: it is
a *maintained change to a prior version* that RE-ENTERS the factory through the
maintenance re-gate path (:mod:`foundry.lifecycle.maintenance`), never a side
door. :func:`evolve` is the runnable, user-facing flow that wires the maintenance
PRIMITIVES end-to-end:

    existing released tree (e.g. a dir written by ``foundry ... --out``)
      + a change request
      -> re-materialize the prior tree + RE-RUN the gates to recover the prior
         attested verdicts (the prior released gate-state)
      -> apply the change -> the new integrated tree
      -> changed_scope + plan_maintenance  (change-PROPORTIONAL re-verification)
      -> maintain                          (RE-GATE via the SAME TwoZoneGateRunner
                                            + RE-ADMIT via the SAME fail-closed
                                            AdmissionController)
      -> EvolveResult                      (the re-gated outcome + the provenance
                                            link to the prior version)

HARD TRUST CONSTRAINT (lifecycle.md §1). An evolved change is NOT trusted because
the prior version passed: it RE-RUNS the gates its change touches (and REUSES the
prior attested verdict for the rest — gov §1f), and the maintained version is
RELEASED only when EVERY required gate RE-PASSES **and** the subject RE-ADMITS.
Fail-closed: a change that does not re-pass / re-admit is NOT released (no PR / no
deploy). :func:`evolve` NEVER bypasses the gates — it delegates the release bit
entirely to :func:`foundry.lifecycle.maintenance.maintain`.

REGRESSION suite. The prior version's acceptance criteria + oracles
(``criteria.json`` + the prior test files) are carried FORWARD into the new tree
as a regression input: the re-gate re-runs Spec/Test over them, so a change that
breaks a prior oracle FAILs the re-gate and is NOT released. :func:`evolve`
surfaces ``regression_passed`` explicitly (the Spec + Test re-verdicts over the
carried-forward suite).

GUARDIAN HITL on protected edits. An edit to an EXISTING criterion / oracle / gate
/ scanner-config is a deterministic CRITICAL-DIFF (:class:`OracleMonitor`,
gov §3 hard rule) — it ESCALATEs to a human. :func:`evolve` runs the SAME
deterministic monitor over the change set; a protected edit BLOCKS the automatic
release (``guardian_escalated=True``) unless the caller supplies an explicit
``guardian_approval`` (the recorded HITL decision). The Guardian decision is
deterministic code, never model judgment.

PROVENANCE chain. The result's ``prior_uri`` links the maintenance chain to the
prior released version (queryable lineage across evolution cycles).

Determinism (L7): :func:`evolve` is a pure function of the prior tree bytes, the
change set, the gate impls, and the runner/admission infra (no clock, no network,
no agent code beyond the gates' own grounding) — equal inputs yield an equal
:class:`EvolveResult`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.gates import (
    AuditGate,
    BuildGate,
    DevGateWriteVerifier,
    SecurityGate,
    SpecGate,
    TestGate,
    TwoZoneGateRunner,
    ZoneBSigner,
)
from foundry.graph.reverification import changed_scope
from foundry.lifecycle.maintenance import (
    MaintenanceOutcome,
    maintain,
    plan_maintenance,
)
from foundry.monitors.oracle_monitor import (
    DEFAULT_CRITERIA_GLOBS,
    DiffEntry,
    OracleMonitor,
)
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.operations import BuildRequest
from foundry.schemas.security import SBOMComponent
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from foundry.gates.runner import GateImpl
    from foundry.monitors.oracle_monitor import CriticalDiffTrip

__all__ = [
    "EvolveRefused",
    "EvolveResult",
    "evolve",
]

# Dev trust-root material (HMAC keys stand in for the production roots; mirrors
# foundry.cli + tests/test_lifecycle_maintenance). The evolve flow shares ONE
# zone-B key across the runner / builder / admission verifier so admission
# re-verifies the runner-attested gate-state under the SAME root.
_KEY = b"dev-zone-b-evolve-key-0123456789-abcd"
_KEY_ID = "dev-key-evolve"
_SCANNER_REV = "scanner-rev-evolve-1"
_LINEAGE_REV = "lineage-rev-evolve-1"
_TOOLCHAIN = "foundry-dev-toolchain-v1"
_BUILDER_ID = "dev-trusted-builder-evolve"

# Pinned hermetic build inputs (dev-grade; §0.3). The maintained subject digest is
# a pure function of the new tree_sha + these pinned inputs.
_SRC = "sha256:" + "c" * 64
_BASE = "sha256:" + "d" * 64
_MAT = "sha256:" + "a" * 64
_COMPONENTS = (SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT, license="MIT"),)

# The default re-gate set — the M1 gate family + the assurance Audit/Security
# gates (the SAME impls a fresh build runs). The whole set must RE-PASS to
# release; the change-proportional plan decides which actually RE-RUN.
_DEFAULT_GATES: tuple[GateImpl, ...] = (
    SpecGate(),
    BuildGate(),
    TestGate(min_passed=1),
    AuditGate(),
    SecurityGate(use_semgrep=False),
)

# The protected-criteria globs the Guardian polices in the evolution route. The
# Spec gate's grounding input is the integrated ``criteria.json`` file (the SAME
# name :func:`foundry.graph.reverification` recognises as the criteria document);
# the monitor's DEFAULT criteria globs match ``*.criteria.json`` / an
# ``acceptance_criteria/`` tree, so we ADD the bare ``criteria.json`` (the file
# the factory actually carries) — an edit to it is still a deterministic
# CRITICAL-DIFF HITL trip, never a weakening of the rule.
_CRITERIA_GLOBS = (*DEFAULT_CRITERIA_GLOBS, "**/criteria.json", "criteria.json")

# The gates whose verdict is the regression signal: Spec re-checks the carried
# acceptance criteria, Test re-runs the carried oracles. A regression FAIL here
# blocks release (it is a required gate, so maintenance already refuses; we ALSO
# surface it as ``regression_passed``).
_REGRESSION_GATES = ("spec", "test")


class EvolveRefused(RuntimeError):
    """An evolved change was not released — fail-closed. (lifecycle.md §1)

    Raised by :func:`evolve` when ``strict=True``: the maintained change did not
    re-pass the gates / re-admit, OR a protected criterion/oracle/gate edit
    ESCALATEd to a human (deterministic HITL) and no ``guardian_approval`` was
    supplied. Evolution never bypasses the gates or the HITL.
    """


@dataclass(frozen=True)
class EvolveResult:
    """The outcome of an evolution cycle — analogous to a :class:`RunResult`.

    Carries only the re-gated decision + content-addressed links (no bodies, L4)
    and makes no external effect. ``released`` is the load-bearing bit: True iff
    the maintained change RE-PASSED every required gate AND RE-ADMITTED AND the
    Guardian did not block (no un-approved protected edit). Fail-closed otherwise.
    """

    change: str
    """The natural-language change request that drove this evolution cycle."""
    prior_uri: str
    """The prior released version (the maintenance-chain link — queryable lineage)."""
    released: bool
    """True iff re-passed the gates + re-admitted + Guardian-clear (fail-closed)."""
    new_subject_digest: str
    """The maintained version's content address (bound to the new tree_sha)."""
    new_state_sha: str
    """The integrated tree_sha of the maintained tree the re-gate ran against."""
    rerun_gates: tuple[str, ...] = ()
    """The gates the change-proportional plan RE-RAN for this change."""
    reuse_gates: tuple[str, ...] = ()
    """The gates whose prior attested verdict was REUSED (unchanged scope)."""
    gate_statuses: dict[str, GateStatus] = field(default_factory=dict)
    """The re-verified integrated gate-state status per gate (re-run + reused)."""
    admission_admitted: bool = False
    """The re-admit decision (the SAME fail-closed AdmissionController)."""
    admission_reason: str = ""
    """The deciding admission ground (``ok`` on ADMIT, else the DENY reason)."""
    regression_passed: bool = False
    """True iff the carried-forward prior criteria/oracles (Spec + Test) RE-PASS."""
    guardian_escalated: bool = False
    """True iff the change touches a protected criterion/oracle/gate path (HITL)."""
    guardian_paths: tuple[str, ...] = ()
    """The protected paths the change touched (the CRITICAL-DIFF matches)."""
    flags: tuple[str, ...] = ()
    """The reasons the release was refused (empty iff released)."""

    @property
    def prior_subject_uri(self) -> str:
        """Alias for the maintenance-chain link (mirrors MaintenanceOutcome)."""
        return self.prior_uri

    def summary(self) -> str:
        """A concise human-readable report of the evolution cycle."""
        lines = [
            f"evolve:    {self.change!r}",
            f"prior:     {self.prior_uri}  (maintenance chain)",
            f"new tree:  {self.new_state_sha}",
            f"re-ran:    {', '.join(self.rerun_gates) or '(none)'}",
            f"reused:    {', '.join(self.reuse_gates) or '(none)'}",
            "re-verified gates:",
        ]
        for name in sorted(self.gate_statuses):
            lines.append(f"  - {name}: {self.gate_statuses[name].value}")
        lines.append(
            f"regression (prior oracles): {'PASS' if self.regression_passed else 'FAIL'}"
        )
        if self.guardian_escalated:
            lines.append(
                "GUARDIAN: protected criterion/oracle/gate edit -> HITL escalation "
                f"({', '.join(self.guardian_paths)})"
            )
        lines.append(
            f"admission: {'ADMIT' if self.admission_admitted else 'DENY'} "
            f"({self.admission_reason})"
        )
        lines.append(
            f"result:    {'RELEASED' if self.released else 'NOT RELEASED (no PR/deploy)'}"
        )
        if not self.released and self.flags:
            lines.append(f"reasons:   {', '.join(self.flags)}")
        return "\n".join(lines)


def _read_tree(root: Path) -> dict[str, str]:
    """Read a materialized source tree to a relative-POSIX-path -> content map.

    Mirrors the map :func:`changed_scope` consumes: every file under ``root``,
    keyed by its forward-slash relative path. The tree is INPUT DATA (read as
    UTF-8, errors replaced) — never executed; no new egress, no trust surface.
    """
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        files[rel] = p.read_text(encoding="utf-8", errors="replace")
    return files


def _apply_changes(
    base: Mapping[str, str], changes: Mapping[str, str | None]
) -> dict[str, str]:
    """Apply a change set (relpath -> new content, or ``None`` to delete) to a tree.

    Pure derivation of the NEW tree's file map from the prior tree's map. A path
    mapped to ``None`` is removed; any other path is added/overwritten. This is
    the deterministic, testable change contract — the same shape the maintenance
    tests drive the engine with (no agent in the loop on this path).
    """
    new = dict(base)
    for rel, body in changes.items():
        if body is None:
            new.pop(rel, None)
        else:
            new[rel] = body
    return new


def _materialize(dest: Path, files: Mapping[str, str]) -> str:
    """Write a file map under ``dest`` and return its integrated ``tree_sha``."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tree_sha(dest, toolchain_digest=_TOOLCHAIN, prefer_git=False)


def _run_gates(
    runner: TwoZoneGateRunner,
    gates: Iterable[GateImpl],
    workdir: Path,
    sha: str,
    run_id: str,
) -> list[GateResult]:
    """Run every gate over ``workdir`` at ``sha`` — the prior attested gate-state."""
    return [
        runner.run(
            g,
            sha,
            Seq(
                run_id=run_id,
                emission_epoch=1,
                superstep_index=0,
                node_id=f"prior/{g.name}",
                local=0,
            ),
            workdir,
        )
        for g in gates
    ]


def _guardian_diff(changes: Mapping[str, str | None]) -> CriticalDiffTrip | None:
    """Deterministic CRITICAL-DIFF check over the change set (gov §3 hard rule).

    Runs the SAME :class:`OracleMonitor` the Guardian uses: ANY changed path that
    matches a protected criterion / oracle / gate / scanner-config glob ESCALATEs
    to a human. Pure (globs, paths) function — no model judgment, no override.
    """
    entries = tuple(DiffEntry(path=rel) for rel in sorted(changes))
    return OracleMonitor(criteria_globs=_CRITERIA_GLOBS).inspect(entries)


def evolve(
    prior_dir: str | Path,
    change: str,
    *,
    changes: Mapping[str, str | None],
    prior_subject_uri: str | None = None,
    gates: Iterable[GateImpl] | None = None,
    guardian_approval: bool = False,
    strict: bool = False,
) -> EvolveResult:
    """Evolve an EXISTING released repo with a change — re-gated, fail-closed.

    Drives the maintenance route end-to-end (lifecycle.md §1/§3):

    1. Read the existing released tree under ``prior_dir`` and RE-RUN every gate
       to recover the prior attested gate-state (the "prior released" verdicts a
       prior :class:`RunResult` would have carried).
    2. Apply ``changes`` (a relpath -> new-content map; ``None`` deletes) to
       produce the NEW integrated tree.
    3. Compute the change set + the change-PROPORTIONAL re-verification plan
       (:func:`plan_maintenance` -> :func:`reverification_plan`): only the gates
       the change touches RE-RUN; the rest REUSE the prior attested verdict.
    4. RE-GATE + RE-ADMIT via :func:`maintain` (the SAME two-zone runner + the
       SAME fail-closed admission controller). The maintained version is RELEASED
       only when every required gate RE-PASSES **and** the subject RE-ADMITS.
    5. Carry the prior criteria/oracles forward as a REGRESSION suite (Spec/Test
       re-run over them; a regression FAIL blocks release).
    6. Guardian-gate edits to existing criteria/oracles/gates (deterministic HITL:
       a protected edit ESCALATEs and blocks release unless ``guardian_approval``).

    Parameters
    ----------
    prior_dir:
        The existing released codebase on disk (e.g. a directory written by
        ``foundry ... --out DIR``). Read-only input.
    change:
        The natural-language change request (carried into the result + summary).
    changes:
        The concrete edit set: a ``relpath -> new content`` mapping (a path mapped
        to ``None`` is deleted). This is the deterministic change contract.
    prior_subject_uri:
        The content-addressed handle of the prior released artifact (the
        maintenance-chain anchor). Defaults to a tree-derived ``cas://`` handle.
    gates:
        The gate impls to re-gate against (defaults to the M1 + assurance family,
        the SAME impls a fresh build runs).
    guardian_approval:
        The recorded HITL decision for a protected criterion/oracle/gate edit. A
        protected edit ESCALATEs (deterministic CRITICAL-DIFF); release is blocked
        UNLESS this is True (the human approved the edit). False (default) means an
        un-approved protected edit blocks the automatic release.
    strict:
        Raise :class:`EvolveRefused` instead of returning a not-released result.

    Returns
    -------
    EvolveResult
        The re-gated incremental outcome + the provenance link to the prior
        version. ``released`` is True iff it re-passed + re-admitted + the Guardian
        did not block.
    """
    from foundry.trust_root import AdmissionController, TrustedBuilder

    gate_list = tuple(gates) if gates is not None else _DEFAULT_GATES
    prior_root = Path(prior_dir)
    run_id = "evolve-run"

    records = GlobalRecords()
    signer = ZoneBSigner(_KEY, _KEY_ID)
    runner = TwoZoneGateRunner(signer, records, _SCANNER_REV, _LINEAGE_REV)
    verifier = DevGateWriteVerifier(_KEY, _KEY_ID, records)
    admission = AdmissionController(builder_id=_BUILDER_ID)

    # 1. The prior released tree + its attested gate-state. We re-materialize the
    #    prior tree into a fresh workdir (canonical layout) and RE-RUN the gates:
    #    those signed verdicts ARE the prior released gate-state the reuse path
    #    folds forward (a cached PASS on an unchanged sub-hash is a real verdict).
    prior_files = _read_tree(prior_root)
    if not prior_files:
        raise ValueError(f"prior repo {str(prior_root)!r} is empty or not a directory")

    new_files = _apply_changes(prior_files, changes)

    # Guardian (deterministic HITL): does the change touch a protected
    # criterion/oracle/gate/scanner path? Computed up front; it blocks the release
    # below unless explicitly approved (it can never SILENTLY clear).
    guardian_trip = _guardian_diff(changes)
    guardian_paths = tuple(guardian_trip.matched) if guardian_trip is not None else ()
    guardian_escalated = guardian_trip is not None

    with tempfile.TemporaryDirectory(prefix="foundry-evolve-") as tmp:
        tmp_root = Path(tmp)
        old_dir = tmp_root / "prior"
        new_dir = tmp_root / "evolved"
        old_sha = _materialize(old_dir, prior_files)
        new_sha = _materialize(new_dir, new_files)

        prior_verdicts = _run_gates(runner, gate_list, old_dir, old_sha, run_id)

        # 2. The change set (content-driven; an identical re-emission is no change).
        scope = changed_scope(
            prior_files,
            new_files,
            old_state_sha=old_sha,
            new_state_sha=new_sha,
            old_toolchain_digest=_TOOLCHAIN,
            new_toolchain_digest=_TOOLCHAIN,
            old_scanner_config_rev=_SCANNER_REV,
            new_scanner_config_rev=_SCANNER_REV,
        )

        anchor = prior_subject_uri or f"cas://evolve/prior/{old_sha}"

        # 3. The change-PROPORTIONAL re-verification plan (RE-RUN vs REUSE).
        plan = plan_maintenance(
            prior_verdicts,
            scope,
            prior_subject_uri=anchor,
            gates=gate_list,
            runner=runner,
            new_state_sha=new_sha,
            scanner_config_rev=_SCANNER_REV,
        )

        # 4. A genuine hermetic build keyed to the maintained tree_sha, then
        #    RE-GATE + RE-ADMIT via maintain (fail-closed; never bypasses).
        builder = TrustedBuilder(signer, builder_id=_BUILDER_ID)
        build = builder.build(
            BuildRequest(
                build_type="dev-build",
                source_uri="git://evolve",
                source_digest=_SRC,
                tree_sha=new_sha,
                base_image_digest=_BASE,
                build_config_uri="cas://invocation/evolve",
                material_digests=(_MAT,),
                hermetic=True,
                seq=Seq(
                    run_id=run_id,
                    emission_epoch=1,
                    superstep_index=0,
                    node_id="build",
                    local=0,
                ),
            ),
            components=_COMPONENTS,
        )
        maint_seq = Seq(
            run_id=run_id,
            emission_epoch=1,
            superstep_index=0,
            node_id="maintain",
            local=0,
        )
        outcome = maintain(
            plan,
            gates=gate_list,
            runner=runner,
            workdir=new_dir,
            new_subject_digest=build.result.subject_digest,
            provenance=build.provenance,
            build_verdict=build.signed_verdict,
            admission=admission,
            records=records,
            verifier=verifier,
            seq=maint_seq,
        )

    return _assemble_result(
        change,
        outcome,
        guardian_escalated=guardian_escalated,
        guardian_paths=guardian_paths,
        guardian_approval=guardian_approval,
        strict=strict,
    )


def _assemble_result(
    change: str,
    outcome: MaintenanceOutcome,
    *,
    guardian_escalated: bool,
    guardian_paths: tuple[str, ...],
    guardian_approval: bool,
    strict: bool,
) -> EvolveResult:
    """Fold the maintenance outcome + the Guardian decision into an EvolveResult.

    The release bit is the maintenance ``released`` AND the Guardian clearance: a
    protected criterion/oracle/gate edit that was NOT approved blocks the release
    even on a green re-gate (the deterministic HITL is non-bypassable).
    """
    statuses: dict[str, GateStatus] = {}
    for g in outcome.gate_results:
        if g.name not in statuses:
            statuses[g.name] = g.status

    # The regression signal: every carried-forward Spec/Test re-verdict PASSed.
    regression_passed = all(
        statuses.get(name) is GateStatus.PASS for name in _REGRESSION_GATES
    )

    flags = list(outcome.flags)
    guardian_blocks = guardian_escalated and not guardian_approval
    if guardian_blocks:
        flags.append("guardian_escalation_unapproved:" + ",".join(guardian_paths))
    released = outcome.released and not guardian_blocks

    result = EvolveResult(
        change=change,
        prior_uri=outcome.prior_uri,
        released=released,
        new_subject_digest=outcome.new_subject_digest,
        new_state_sha=outcome.new_state_sha,
        rerun_gates=outcome.rerun_gates,
        reuse_gates=outcome.reuse_gates,
        gate_statuses=statuses,
        admission_admitted=outcome.admission_admitted,
        admission_reason=outcome.admission_reason,
        regression_passed=regression_passed,
        guardian_escalated=guardian_escalated,
        guardian_paths=guardian_paths,
        flags=tuple(flags),
    )
    if strict and not released:
        raise EvolveRefused(
            f"evolved change {change!r} to {outcome.prior_uri!r} was NOT released "
            f"{result.flags!r} — fail-closed (evolution never bypasses the gates "
            "or the HITL; lifecycle.md §1)"
        )
    return result
