"""Build upgrade & migration with runs in flight (operations.md §5/§8).

Long-horizon runs span hours/days, so a deploy or a provider model rotation
*will* land mid-run. The load-bearing Day-2 invariant: **a version upgrade must
not corrupt or strand an in-flight run.** Every checkpoint carries a
:class:`~foundry.schemas.operations.VersionStamp`; this module is the
deterministic planner that decides, per in-flight run, whether it:

* **DRAINS** on its pinned (old) version — complete-then-upgrade, the safe
  default for a graph/prompt-only change (operations.md §5: "a run resumes on
  its pinned graph/prompt version; the new version applies only to new runs");
* **MIGRATES** — an *additive* state-schema change ships a migration function
  (``migration_uri``) that rewrites the checkpoint forward; or
* **PAUSES** — fail-closed. A breaking schema change with no safe migration, or
  a checkpoint that cannot be loaded under the serde allowlist, NEVER silently
  upgrades. The run halts to the operator (flagged via ``affected_run_ids`` +
  an :class:`~foundry.schemas.operations.UpgradeAction`), so the choice is
  *pause-and-ask*, never *lossy-resume* (operations.md §5: "a run never resumes
  against an incompatible schema — it fails to the operator instead of silently
  mis-resuming").

Compatibility rules (operations.md §5):

* Same ``state_schema_ver`` ⇒ schema-compatible. A graph/prompt/model-pin
  difference is fine: the run resumes on its PINNED graph/prompt (the new
  version applies to new runs only), so this DRAINS.
* ``target`` schema strictly NEWER than ``current`` ⇒ a migration is required.
  Additive (``migration_uri`` provided) ⇒ MIGRATE; breaking (no migration) ⇒
  DRAIN if the run can complete on the old version without re-pinning, else
  PAUSE. A run mid-schema whose checkpoint cannot be migrated PAUSES.
* ``target`` schema OLDER than ``current`` ⇒ a downgrade: never auto-migrated
  (forward-only); PAUSE for operator action.

Determinism: pure functions, no clock, no I/O, no LLM. The same inputs always
produce the same :class:`UpgradePlan` set, so the plan is reproducible and
auditable. ``migrate_checkpoint`` is a **dev-grade seam** — it reuses the serde
allowlist to prove a checkpoint round-trips under the target, but a real
state-schema migration (rewriting channel shapes) is an ops task wiring a
concrete migration function behind ``migration_uri`` (noted inline).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from foundry.durability.serde import FOUNDRY_MSGPACK_MODULES
from foundry.schemas.operability import (
    UpgradeAction,
    UpgradePlan,
    VersionStamp,
)
from foundry.state.seq import Seq

__all__ = [
    "InFlightRun",
    "MigrationError",
    "RunUpgradeDecision",
    "compatible",
    "migrate_checkpoint",
    "plan_fleet_upgrade",
    "plan_upgrade",
    "schema_compatible",
    "serde_loadable",
]


class MigrationError(Exception):
    """A checkpoint cannot be safely migrated/loaded under the target version.

    Raised by :func:`migrate_checkpoint` (fail-closed). The planner catches it
    and routes the run to PAUSE rather than ever returning a lossy/partial
    migration — never a silent upgrade (operations.md §5).
    """


@dataclass(frozen=True)
class InFlightRun:
    """One run in flight at upgrade time (data input to the planner).

    ``stamp`` is the VersionStamp pinned on the run's checkpoint/manifest.
    ``can_complete_on_old`` is whether the run can run to terminal on its
    pinned (old) version WITHOUT re-pinning onto the target — the drain
    precondition (operations.md §5). ``checkpoint_model_types`` are the frozen
    model type names the run's checkpoint carries (e.g. ``"FeatureDiff"``); the
    migration seam checks each is still on the serde allowlist so an upgrade
    that drops/renames a model cannot orphan an in-flight checkpoint. ``seq``
    orders the produced decision deterministically.
    """

    run_id: str
    stamp: VersionStamp
    seq: Seq
    can_complete_on_old: bool = True
    checkpoint_model_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunUpgradeDecision:
    """The per-run outcome of :func:`plan_upgrade`.

    ``plan`` is the typed :class:`UpgradePlan` channel record. ``stranded`` is
    True ONLY for the PAUSE outcome — the run is neither drained nor migrated and
    is flagged for operator action; it is never corrupted (the checkpoint is left
    untouched on its pinned version). The two safe outcomes (DRAIN/MIGRATE) carry
    ``stranded == False``. ``reason`` is the operator-facing explanation.
    """

    run_id: str
    plan: UpgradePlan
    reason: str
    stranded: bool = field(default=False)

    @property
    def action(self) -> UpgradeAction:
        return self.plan.action

    @property
    def paused(self) -> bool:
        """A PAUSE outcome — fail-closed, flagged for the operator (§5)."""
        return self.plan.action is UpgradeAction.ABORT


# The set of frozen-model type names the serde allowlist round-trips. A model
# NOT here degrades to a dict on load and is rejected fail-closed by the store
# (serde.py docstring), so an upgrade that removes one would orphan any in-flight
# checkpoint carrying it — the migration seam refuses such a checkpoint.
_ALLOWLISTED_TYPES: frozenset[str] = frozenset(
    name for _module, name in FOUNDRY_MSGPACK_MODULES
)


def serde_loadable(model_types: Sequence[str]) -> bool:
    """True iff every model the checkpoint carries is on the serde allowlist.

    An upgrade that changes a model must keep old checkpoints loadable (or
    migrate them); a type missing from the allowlist degrades to ``dict`` on
    load and is rejected fail-closed (serde.py), so a checkpoint carrying it
    cannot be safely resumed under the target. Pure.
    """
    return all(t in _ALLOWLISTED_TYPES for t in model_types)


def schema_compatible(stamp: VersionStamp, target: VersionStamp) -> bool:
    """True iff ``stamp``'s state-schema needs NO migration to run on ``target``.

    Same ``state_schema_ver`` ⇒ schema-compatible (a graph/prompt/model-pin
    delta is handled by resume-on-pinned-version, not a schema migration). A
    differing schema version is NOT schema-compatible — it needs a migration
    function or a drain. Pure.
    """
    return stamp.state_schema_ver == target.state_schema_ver


def compatible(stamp: VersionStamp, target: VersionStamp) -> bool:
    """The version-stamp compatibility check (operations.md §5).

    A run stamped ``stamp`` can resume directly against ``target`` iff its
    state-schema is compatible AND its model pins do not conflict with the
    target's. Model pins only conflict when BOTH stamps pin the SAME role to
    DIFFERENT ids (a mid-flight model-drift the run did not opt into); a role
    the target adds (or the run never pinned) is not a conflict — new runs
    re-pin, in-flight runs keep their pin. Graph/prompt versions intentionally
    do NOT gate compatibility: a run resumes on its PINNED graph/prompt, so a
    bump there is a new-runs-only change, not an incompatibility. Pure.
    """
    if not schema_compatible(stamp, target):
        return False
    return not _pins_conflict(stamp.model_pins, target.model_pins)


def _pins_conflict(old: Mapping[str, str], new: Mapping[str, str]) -> bool:
    """True iff a role is pinned to different ids on both sides (drift)."""
    return any(role in new and new[role] != pid for role, pid in old.items())


def migrate_checkpoint(
    stamp: VersionStamp,
    target: VersionStamp,
    *,
    model_types: Sequence[str] = (),
    migration_uri: str | None = None,
) -> VersionStamp:
    """Dev-grade checkpoint-migration seam (operations.md §5; reuses serde).

    Returns the post-migration :class:`VersionStamp` (i.e. ``target``) when the
    checkpoint can be safely carried forward; raises :class:`MigrationError`
    (fail-closed) otherwise — the planner turns that into a PAUSE, never a
    silent lossy upgrade.

    A checkpoint is migratable iff:

    * its models are all still on the serde allowlist (:func:`serde_loadable`) —
      an upgrade that dropped/renamed a model would orphan the checkpoint;
    * the move is forward-only (``target`` schema ≥ ``stamp`` schema) — a
      downgrade is never auto-migrated;
    * for a STRICT schema bump, a ``migration_uri`` is supplied (the additive
      migration function); a same-schema "migration" is a no-op carry-forward.

    NOTE (ops task): this seam proves the checkpoint round-trips under the
    target's allowlist; it does NOT execute a concrete channel-rewriting
    migration. A real additive migration wires a deterministic function behind
    ``migration_uri`` (rewriting old-shape channels to the new shape) and is run
    by the trusted (non-agent) upgrade tooling, version-stamped per §8 — not
    here. Pure.
    """
    if not serde_loadable(model_types):
        raise MigrationError(
            f"checkpoint carries non-allowlisted model(s); cannot load under "
            f"target schema {target.state_schema_ver} (run would be orphaned)"
        )
    if target.state_schema_ver < stamp.state_schema_ver:
        raise MigrationError(
            f"downgrade {stamp.state_schema_ver}->{target.state_schema_ver} is "
            f"not auto-migratable (forward-only)"
        )
    if target.state_schema_ver > stamp.state_schema_ver and migration_uri is None:
        raise MigrationError(
            f"breaking schema bump {stamp.state_schema_ver}->"
            f"{target.state_schema_ver} has no migration function (drain required)"
        )
    return target


def plan_upgrade(
    current: VersionStamp,
    target: VersionStamp,
    run: InFlightRun,
    *,
    migration_uri: str | None = None,
) -> RunUpgradeDecision:
    """Decide one in-flight run's upgrade outcome — DRAIN / MIGRATE / PAUSE.

    The decision order is fail-closed (no in-flight run is ever corrupted or
    stranded by a silent upgrade; operations.md §5):

    1. **Compatible** (same schema, no pin conflict) ⇒ **DRAIN**
       (``UpgradeAction.DRAIN_BEFORE_UPGRADE``): the run resumes on its pinned
       graph/prompt and completes on the old version; the new version applies to
       new runs only. The safest outcome.
    2. **Additive schema bump** with a ``migration_uri`` AND a serde-loadable
       checkpoint ⇒ **MIGRATE** (``UpgradeAction.MIGRATE``): the checkpoint is
       carried forward by the migration function (proven via
       :func:`migrate_checkpoint`).
    3. Otherwise — a breaking schema change, a downgrade, an unloadable
       checkpoint, or a model-pin drift the run cannot drain through ⇒
       **PAUSE** (``UpgradeAction.ABORT``, ``stranded=True``): the run halts to
       the operator with its checkpoint left intact on its pinned version. If the
       run CAN complete on the old version we still prefer DRAIN over PAUSE (a
       breaking change drains the in-flight cohort before the upgrade); only a run
       that can NEITHER drain NOR migrate pauses.

    The per-run ``run.stamp`` is the authority for what the run is on; ``current``
    is the fleet's pre-upgrade version (used only to scope the cohort). Pure /
    deterministic. The returned :class:`UpgradePlan` carries the seq from
    ``run.seq`` so the channel record is ordered.
    """
    if compatible(run.stamp, target):
        return _decision(
            run,
            target,
            action=UpgradeAction.DRAIN_BEFORE_UPGRADE,
            reason="compatible: resume on pinned graph/prompt; drain on old version",
            stranded=False,
        )

    # Schema differs (or a pin conflicts). Try an additive migration first.
    additive = (
        target.state_schema_ver > run.stamp.state_schema_ver
        and migration_uri is not None
    )
    if additive:
        try:
            migrate_checkpoint(
                run.stamp,
                target,
                model_types=run.checkpoint_model_types,
                migration_uri=migration_uri,
            )
        except MigrationError as exc:
            return _decision(
                run,
                target,
                action=UpgradeAction.ABORT,
                reason=f"unmigratable checkpoint, paused for operator: {exc}",
                stranded=True,
                migration_uri=migration_uri,
            )
        return _decision(
            run,
            target,
            action=UpgradeAction.MIGRATE,
            reason="additive schema bump; checkpoint migrated forward",
            stranded=False,
            migration_uri=migration_uri,
        )

    # Breaking change / downgrade / pin-drift: drain the run on its old version
    # if it can complete there; otherwise it can be neither drained nor migrated
    # and must PAUSE fail-closed (never a lossy resume) — but only if its
    # checkpoint is even loadable; a corrupt/orphaned checkpoint always pauses.
    if run.can_complete_on_old and serde_loadable(run.checkpoint_model_types):
        return _decision(
            run,
            target,
            action=UpgradeAction.DRAIN_BEFORE_UPGRADE,
            reason="breaking change; drain on old version before upgrade",
            stranded=False,
        )

    reason = (
        "breaking change cannot be drained and no migration; "
        "paused for operator (checkpoint intact, never corrupted)"
    )
    if not serde_loadable(run.checkpoint_model_types):
        reason = (
            "checkpoint carries non-allowlisted model(s) under target; "
            "paused for operator (never silently mis-resumed)"
        )
    return _decision(
        run,
        target,
        action=UpgradeAction.ABORT,
        reason=reason,
        stranded=True,
        migration_uri=migration_uri,
    )


def plan_fleet_upgrade(
    current: VersionStamp,
    target: VersionStamp,
    in_flight_runs: Sequence[InFlightRun],
    *,
    migration_uri: str | None = None,
) -> tuple[RunUpgradeDecision, ...]:
    """Plan the upgrade across the whole in-flight cohort (operations.md §5/§8).

    Maps :func:`plan_upgrade` over every in-flight run; the result is ordered by
    each run's ``seq`` so the fleet plan is deterministic regardless of input
    order (a correlated upgrade hits all in-flight runs at once, §5). No run is
    dropped: the output has exactly one decision per input run, so a planner can
    never silently lose an in-flight run. Pure.
    """
    decisions = [
        plan_upgrade(current, target, run, migration_uri=migration_uri)
        for run in in_flight_runs
    ]
    decisions.sort(key=lambda d: d.plan.seq)
    return tuple(decisions)


def _decision(
    run: InFlightRun,
    target: VersionStamp,
    *,
    action: UpgradeAction,
    reason: str,
    stranded: bool,
    migration_uri: str | None = None,
) -> RunUpgradeDecision:
    """Assemble the per-run decision + its typed UpgradePlan (single site)."""
    plan = UpgradePlan(
        from_version=run.stamp,
        to_version=target,
        action=action,
        affected_run_ids=(run.run_id,),
        migration_uri=migration_uri if action is UpgradeAction.MIGRATE else None,
        seq=run.seq,
    )
    return RunUpgradeDecision(
        run_id=run.run_id, plan=plan, reason=reason, stranded=stranded
    )
