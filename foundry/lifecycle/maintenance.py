"""Generated-code maintenance lifecycle — re-verify + re-gate, no bypass. (lifecycle.md §1)

Software is owned for years, not shipped once. A maintained/evolved change to a
previously-released artifact is NOT trusted just because the prior version was:
it is RE-VERIFIED **change-proportionally** and must RE-PASS the trust gates +
RE-ADMIT before the maintained version is released. Maintenance NEVER bypasses
the gates — fail-closed: a maintained change that does not re-pass is NOT
released (governance.md §1f/§5, lifecycle.md §1).

Reuse, not reinvention:

* the change-proportional re-verification is the EXISTING engine
  (:func:`foundry.graph.reverification.reverification_plan`) — a maintained
  change re-runs only the gates whose scope the change touches and REUSES the
  prior attested verdict for the rest (a cached PASS keyed on an unchanged
  sub-hash is still a real runner-attested verdict; gov §1f). We do not
  re-derive a parallel re-verify;
* the re-gate is the SAME two-zone runner (:class:`TwoZoneGateRunner`) — the
  maintained version's verdicts are zone-B-attested exactly as a fresh build's;
* the re-admit is the SAME fail-closed deploy gate
  (:class:`~foundry.trust_root.AdmissionController`).

Provenance of the maintained version LINKS to the prior version — a maintenance
chain (``MaintenanceOutcome.prior_uri`` / the released
:class:`~foundry.lifecycle.provenance.ProvenanceRecord.prior_uri`), so a
long-lived artifact's lineage is queryable across evolution cycles.

Determinism (L7): :func:`maintain` is a pure function of the prior verdicts +
the change set + the gate impls + the runner/admission infra (no clock, no
network, no agent code beyond the gates' own grounding), so the same inputs
yield the same :class:`MaintenanceOutcome`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foundry.graph.reverification import ReverificationPlan, reverification_plan
from foundry.schemas.base import FrozenModel

# Runtime imports: FIELD types on the frozen pydantic models below need the
# concrete classes at model-build time (a TYPE_CHECKING-only import leaves the
# forward refs unresolved). All are import-cheap.
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.operations import AdmissionVerdict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from foundry.gates.attestation import DevGateWriteVerifier
    from foundry.gates.runner import GateImpl, TwoZoneGateRunner
    from foundry.graph.reverification import ChangedScope
    from foundry.schemas.security import SLSAProvenance
    from foundry.state.global_records import GlobalRecordsView
    from foundry.state.seq import Seq
    from foundry.trust_root.admission import AdmissionController

__all__ = [
    "MaintenanceOutcome",
    "MaintenancePlan",
    "MaintenanceRefused",
    "maintain",
    "plan_maintenance",
]


class MaintenanceRefused(RuntimeError):
    """A maintained change did not re-pass the gates / re-admit — fail-closed.

    Raised by :func:`maintain` when ``strict`` is set: maintenance never
    bypasses the trust gates, so a maintained version that does not RE-PASS the
    re-verification or RE-ADMIT is REFUSED, never released (lifecycle.md §1).
    """


class MaintenancePlan(FrozenModel):
    """The plan for re-verifying ONE maintained change (lifecycle.md §1).

    Wraps the change-proportional :class:`ReverificationPlan` (which gates RE-RUN
    vs REUSE for this change) plus the maintenance-chain link to the prior
    released version. The plan is a pure derivation; :func:`maintain` executes it
    (re-runs the RE-RUN gates, folds the REUSED prior verdicts, re-admits).

    ``required_gates`` is the gate set the release demands PASS — the SAME set
    admission re-verifies. ``rerun_gates`` / ``reuse_gates`` expose the
    change-proportional split for inspection.
    """

    prior_subject_uri: str  # the previously-released version being maintained
    new_state_sha: str  # the integrated tree_sha of the maintained tree
    required_gates: tuple[str, ...]
    reverification: ReverificationPlan

    @property
    def rerun_gates(self) -> tuple[str, ...]:
        return self.reverification.rerun_gates

    @property
    def reuse_gates(self) -> tuple[str, ...]:
        return self.reverification.reuse_gates


class MaintenanceOutcome(FrozenModel):
    """The result of maintaining one change — released iff it re-passed + re-admitted.

    ``released`` is the load-bearing bit: True iff every required gate RE-PASSED
    (re-run verdicts attested PASS + reused prior attested PASS verdicts) AND the
    maintained subject RE-ADMITTED. Fail-closed: any non-PASS required gate or a
    DENY admission ⇒ ``released=False`` with the reasons in ``flags`` and
    ``admission_reason`` — the maintained version is NOT released.

    ``gate_results`` is the full re-verified integrated gate-state (re-run +
    reused), every record runner-attested and keyed to ``new_state_sha`` (gov
    §1f). ``prior_uri`` links the maintenance chain to the previously-released
    version.
    """

    prior_subject_uri: str  # the prior released version (maintenance chain link)
    new_subject_digest: str  # the maintained version's content address
    new_state_sha: str  # the integrated tree_sha of the maintained tree
    released: bool  # True iff re-passed the gates AND re-admitted (fail-closed)
    gate_results: tuple[GateResult, ...] = ()  # re-verified integrated gate-state
    rerun_gates: tuple[str, ...] = ()  # gates re-run for this change
    reuse_gates: tuple[str, ...] = ()  # gates whose prior verdict was reused
    admission_admitted: bool = False  # the re-admit decision
    admission_reason: str = ""  # the deciding admission ground (ok / deny reason)
    flags: tuple[str, ...] = ()  # reasons the release was refused (empty iff released)

    @property
    def prior_uri(self) -> str:
        """Alias for the maintenance-chain link (the prior released version)."""
        return self.prior_subject_uri


def plan_maintenance(
    prior_verdicts: Iterable[GateResult],
    changed: ChangedScope,
    *,
    prior_subject_uri: str,
    gates: Iterable[GateImpl],
    runner: TwoZoneGateRunner,
    new_state_sha: str,
    scanner_config_rev: str,
    required_gates: tuple[str, ...] | None = None,
) -> MaintenancePlan:
    """Derive the change-proportional re-verification plan for a maintained change.

    Pure: reuses :func:`reverification_plan` to decide RE-RUN vs REUSE per gate
    (a one-file change re-runs only the gates whose scope it touches; the rest
    REUSE the prior attested verdict — gov §1f), and links the maintenance chain
    to ``prior_subject_uri``. ``required_gates`` defaults to the names of every
    gate in ``gates`` (the whole set must re-pass).
    """
    gate_list = list(gates)
    plan = reverification_plan(
        prior_verdicts,
        changed,
        gates=gate_list,
        runner=runner,
        new_state_sha=new_state_sha,
        scanner_config_rev=scanner_config_rev,
    )
    req = required_gates if required_gates is not None else tuple(g.name for g in gate_list)
    return MaintenancePlan(
        prior_subject_uri=prior_subject_uri,
        new_state_sha=new_state_sha,
        required_gates=req,
        reverification=plan,
    )


def _gate_seq(base: Seq, name: str, local: int) -> Seq:
    """A per-gate Seq for a re-run verdict (distinct local per gate)."""
    return base._replace(node_id=f"maintain/{name}", local=local)


def maintain(
    plan: MaintenancePlan,
    *,
    gates: Iterable[GateImpl],
    runner: TwoZoneGateRunner,
    workdir: Path | str,
    new_subject_digest: str,
    provenance: SLSAProvenance,
    build_verdict: GateResult,
    admission: AdmissionController,
    records: GlobalRecordsView,
    verifier: DevGateWriteVerifier,
    seq: Seq,
    expected_gate_trees: Mapping[str, str] | None = None,
    scanner_config_rev: str | None = None,
    strict: bool = False,
) -> MaintenanceOutcome:
    """Execute a maintenance plan: RE-RUN the touched gates, REUSE the rest,
    require every required gate to RE-PASS, then RE-ADMIT — fail-closed.

    The maintained version is released ONLY when BOTH hold for the EXACT
    ``new_subject_digest``:

    1. **Re-verified gate-state re-passes.** For each gate the plan marks RE-RUN,
       run it through the SAME two-zone runner (zone-B-attested verdict, keyed to
       ``plan.new_state_sha``); for each gate it marks REUSE, fold the prior
       attested verdict forward verbatim (a cached PASS on an unchanged sub-hash
       is a real verdict — gov §1f). Every gate in ``plan.required_gates`` must
       be present and attested **PASS** — a non-PASS (or absent) required gate
       means the maintained change did not clear the gates ⇒ NOT released.
    2. **Re-admits.** The maintained subject re-admits via the SAME fail-closed
       :class:`AdmissionController` (provenance binds the subject, every required
       gate re-verifies + PASSes for its tree, the digest is not disqualified). A
       DENY ⇒ NOT released.

    Maintenance NEVER bypasses the gates: a maintained change that fails ANY
    required gate or admission is REFUSED (``released=False``; ``strict=True``
    raises :class:`MaintenanceRefused`). The outcome's ``prior_uri`` links the
    maintenance chain to the previously-released version.
    """
    by_name = {g.name: g for g in gates}
    work = Path(workdir)

    # 1a. Re-verify: re-run the touched gates, reuse the rest. The union covers
    #     the WHOLE tree (re-run fresh verdicts + reused prior attested verdicts).
    results: dict[str, GateResult] = {}
    for i, decision in enumerate(plan.reverification.decisions):
        name = decision.gate_name
        if decision.rerun:
            gate = by_name.get(name)
            if gate is None:
                # A gate the plan wants re-run but no impl was supplied — cannot
                # re-verify it; fail-closed (it will read as a missing required
                # gate below).
                continue
            results[name] = runner.run(
                gate, plan.new_state_sha, _gate_seq(seq, name, i), work
            )
        elif decision.reused_verdict is not None:
            results[name] = decision.reused_verdict

    # 1b. Every required gate must be present + attested PASS (fail-closed).
    flags: list[str] = []
    all_pass = True
    for name in plan.required_gates:
        g = results.get(name)
        if g is None:
            flags.append(f"required_gate_missing:{name}")
            all_pass = False
        elif g.status is not GateStatus.PASS:
            flags.append(f"required_gate_not_pass:{name}")
            all_pass = False

    gate_state = tuple(results.values())

    # The re-verified gate-state legitimately SPANS TWO trees (gov §1f): the
    # RE-RUN gates attest the new integrated tree, while a REUSED gate carries
    # forward a prior verdict still bound to the OLD sub-tree it ran against (a
    # cached PASS on an unchanged sub-hash is a real attested verdict). Admission
    # is input-bound per gate (AW2 multi-tree path), so we pin EACH gate to the
    # tree its verdict actually attests — the re-run gates to ``new_state_sha``,
    # the reused gates to their recorded ``run_against`` — rather than forcing a
    # single whole-tree binding that would spuriously reject the reused verdicts.
    # An explicit caller-supplied mapping (a release spanning a distinct
    # assurance tree) takes precedence.
    derived_trees: dict[str, str] = {g.name: g.run_against for g in gate_state}
    if expected_gate_trees is not None:
        derived_trees.update(expected_gate_trees)

    # 2. Re-admit the maintained subject (only meaningful if the gate-state is
    #    complete + green; a failed re-verify is already not released, but we
    #    still surface the admission decision honestly).
    admission_decision = admission.admit(
        new_subject_digest,
        provenance=provenance,
        build_verdict=build_verdict,
        gate_results=gate_state,
        required_gates=plan.required_gates,
        records=records,
        verifier=verifier,
        seq=seq,
        expected_gate_trees=derived_trees,
        scanner_config_rev=scanner_config_rev,
    )
    admitted = admission_decision.verdict is AdmissionVerdict.ADMIT
    if not admitted:
        flags.append(f"admission_denied:{admission_decision.reason}")

    released = all_pass and admitted
    outcome = MaintenanceOutcome(
        prior_subject_uri=plan.prior_subject_uri,
        new_subject_digest=new_subject_digest,
        new_state_sha=plan.new_state_sha,
        released=released,
        gate_results=gate_state,
        rerun_gates=plan.rerun_gates,
        reuse_gates=plan.reuse_gates,
        admission_admitted=admitted,
        admission_reason=admission_decision.reason,
        flags=tuple(flags),
    )
    if strict and not released:
        raise MaintenanceRefused(
            f"maintained change to {plan.prior_subject_uri!r} did not re-pass the "
            f"gates / re-admit {outcome.flags!r} — fail-closed, NOT released "
            "(maintenance never bypasses the gates; lifecycle.md §1)"
        )
    return outcome
