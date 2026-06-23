"""ML/data `state_sha` data/weight hooks + the compute-cost budget dimension.

The ML/data domain (domains.md §2-3) keys a class-3 / ML verdict on **`state_sha`**
rather than the source-only `tree_sha`: a data, feature, or weight change must
RE-KEY the verdict so the dependency-scoped re-verification engine re-draws it
instead of laundering a stale "already verified" PASS forward.

Two facilities live here:

1. **state_sha data/weight hooks** (domains.md §2). A pack declares its data /
   weight / feature refs via the ``state_inputs`` hook
   (:class:`~foundry.domains.contract.StateInputPolicy`); :func:`ml_state_sha`
   folds those *content-identity* refs into the existing
   :func:`foundry.state.tree_sha.state_sha` formula
   ``H(source ⧺ lockfile ⧺ toolchain ⧺ data_refs ⧺ feature_defs ⧺ weight_refs)``.
   The data/weight bytes stay in their external versioned stores; only the
   verifiable content identity enters the hash. A change to ANY ref set produces
   a different key, invalidating the cached verdict (forcing a re-draw).

   **Fail-closed:** an ML verdict that does not bind ANY data/weight ref into its
   key is invalid (:class:`UnboundMLStateError`) — a verdict with no data binding
   could be silently reused across a data/weight change. :func:`ml_state_sha`
   refuses to mint such a key; :func:`ml_verdict_rebinds` is the predicate that
   detects whether two ref sets share a key (a cache hit) or re-key (a re-draw).

2. **compute-cost budget dimension** (domains.md §3). Expensive ML gates (a
   retrain, a full device-matrix visual run) consume COMPUTE — an integer budget
   dimension DISTINCT from the USD spend tracked by
   :mod:`foundry.governance.budget_controller`. :class:`ComputeBudget` /
   :class:`ComputeBudgetController` are the analogue of ``BudgetCaps`` /
   ``BudgetController`` for that dimension: integer compute units, a cap, a
   per-gate charge, and a fail-closed decision when a charge would exceed the cap
   (the gate PAUSES / escalates rather than silently overspending compute).

Everything here is PURE / DETERMINISTIC (the digests fold canonical bytes; the
budget accounting is integer arithmetic) and reuses the ``tree_sha`` +
``budget_controller`` patterns verbatim rather than rebuilding them.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field

from foundry.domains.contract import StateInputPolicy
from foundry.schemas.base import FrozenModel
from foundry.state.tree_sha import state_sha

__all__ = [
    "ComputeBudget",
    "ComputeBudgetController",
    "ComputeChargeDecision",
    "ComputeOverspendError",
    "UnboundMLStateError",
    "ml_state_sha",
    "ml_state_sha_from_policy",
    "ml_verdict_rebinds",
]


# --------------------------------------------------------------------------- #
# state_sha data/weight hooks (domains.md §2)
# --------------------------------------------------------------------------- #


class UnboundMLStateError(ValueError):
    """An ML verdict was keyed without binding ANY data/weight ref (fail-closed).

    A class-3 / ML verdict's reuse key MUST include its data + weight refs so a
    data or weight change re-keys it. A key derived from zero data/weight/feature
    refs is the source-only ``tree_sha`` — reusable across a data change — so
    minting an *ML* key from it would silently launder a stale PASS. :func:`ml_state_sha`
    raises this rather than returning such a key (domains.md §2).
    """


def ml_state_sha(
    base_tree_sha: str,
    *,
    data_refs: Iterable[str] = (),
    weight_refs: Iterable[str] = (),
    feature_defs: Iterable[str] = (),
    require_binding: bool = True,
) -> str:
    """Fold a pack's data/weight/feature refs into an ML ``state_sha`` key.

    ``base_tree_sha`` is the source-only :func:`foundry.state.tree_sha.tree_sha`
    (``H(source ⧺ lockfile ⧺ toolchain)``); this binds the declared
    *content-identity* refs on top, reusing :func:`foundry.state.tree_sha.state_sha`'s
    ref support (each set is sorted + deduplicated there, so caller ordering and
    duplication never perturb the key). A change to ANY ref set yields a different
    key — invalidating the cached verdict / forcing a re-draw.

    Fail-closed (``require_binding=True``, the default): if NO data, weight, OR
    feature ref is supplied the result would equal ``base_tree_sha`` — a key
    reusable across a data/weight change — so this raises
    :class:`UnboundMLStateError` rather than mint an unbound ML key. Pass
    ``require_binding=False`` ONLY for the source-only / non-ML special case.
    """
    data = tuple(data_refs)
    weights = tuple(weight_refs)
    features = tuple(feature_defs)
    if require_binding and not (data or weights or features):
        raise UnboundMLStateError(
            "an ML verdict must bind at least one data/weight/feature ref into its "
            "state_sha; a key over zero refs is the source-only tree_sha (reusable "
            "across a data/weight change) — fail-closed (domains.md §2)"
        )
    # Reuse tree_sha.state_sha verbatim: base_tree_sha rides as the `source_tree`
    # component; lockfile/toolchain are already folded into base_tree_sha, so we
    # pass neutral constants for them (they do not vary across an ML re-key — the
    # ref sets are what re-key the verdict). Keeping them as fixed, named markers
    # makes the key a pure function of (base, refs).
    return state_sha(
        source_tree=base_tree_sha,
        lockfile_hash="ml_state_sha:lockfile_in_base",
        toolchain_digest="ml_state_sha:toolchain_in_base",
        data_refs=data,
        feature_defs=features,
        weight_refs=weights,
    )


def ml_state_sha_from_policy(
    base_tree_sha: str,
    policy: StateInputPolicy,
    *,
    require_binding: bool = True,
) -> str:
    """:func:`ml_state_sha` keyed off a composed :class:`StateInputPolicy`.

    The ``state_inputs`` hook composes (strictest-wins, pure union) the declared
    data/feature/weight refs into a :class:`StateInputPolicy`; this folds that
    composed ref set into the ML ``state_sha``. Same fail-closed binding rule.
    """
    return ml_state_sha(
        base_tree_sha,
        data_refs=policy.data_refs,
        weight_refs=policy.weight_refs,
        feature_defs=policy.feature_defs,
        require_binding=require_binding,
    )


def ml_verdict_rebinds(
    base_tree_sha: str,
    *,
    old: StateInputPolicy,
    new: StateInputPolicy,
) -> bool:
    """True iff ``new`` refs RE-KEY the verdict vs ``old`` (a re-draw, not a hit).

    The dependency-scoped re-verification engine reuses a cached verdict only when
    the key is identical. A data / weight / feature change yields a different
    :func:`ml_state_sha`, so this returns ``True`` (the cached verdict is
    invalidated → forced re-draw); identical refs return ``False`` (a legitimate
    cache hit). ``require_binding=False`` here because this is a key COMPARISON,
    not a verdict mint — either side may be the empty (source-only) base.
    """
    return ml_state_sha_from_policy(
        base_tree_sha, old, require_binding=False
    ) != ml_state_sha_from_policy(base_tree_sha, new, require_binding=False)


# --------------------------------------------------------------------------- #
# compute-cost budget dimension (domains.md §3)
# --------------------------------------------------------------------------- #


class ComputeBudget(FrozenModel):
    """A compute-unit cap for expensive ML/visual gates (domains.md §3).

    Compute is an integer dimension DISTINCT from USD spend
    (:mod:`foundry.governance.budget_controller` owns micro-USD). One compute unit
    is a pack-defined quantum (a GPU-minute, a retrain slice, a device-matrix
    cell); ``cap_units`` is the absolute ceiling and ``per_gate_units`` maps an
    expensive gate name -> its charge. A gate not in the map charges
    ``default_gate_units``.
    """

    cap_units: int
    per_gate_units: dict[str, int] = Field(default_factory=dict)
    default_gate_units: int = 1

    def charge_for(self, gate: str) -> int:
        """The compute units gate ``gate`` costs to run."""
        return self.per_gate_units.get(gate, self.default_gate_units)


class ComputeChargeDecision(FrozenModel):
    """Whether an expensive gate may run within the compute budget (a boundary)."""

    allowed: bool
    gate: str
    reason: str
    charge_units: int
    spent_units: int
    remaining_units: int


class ComputeOverspendError(RuntimeError):
    """A compute charge was COMMITTED past the cap (fail-closed booking guard).

    :meth:`ComputeBudgetController.charge` raises this rather than book a spend
    that crosses ``cap_units`` — an expensive gate pauses / escalates rather than
    silently overspending compute (domains.md §3). Callers that want to DECIDE
    without committing use :meth:`ComputeBudgetController.authorize` first.
    """


class ComputeBudgetController:
    """Single-writer compute-budget authority (the USD ``BudgetController`` analogue).

    Accounting is integer compute units. ``authorize(gate)`` decides (without
    committing) whether the next expensive-gate run fits under the cap;
    ``charge(gate)`` books it and FAILS CLOSED when it would exceed the cap (it
    raises rather than overspend — the gate escalates). Pure/deterministic: the
    decision is a function of (cap, prior spend, this gate's charge).
    """

    def __init__(self, budget: ComputeBudget) -> None:
        if budget.cap_units < 0:
            raise ValueError("cap_units must be >= 0")
        self.budget = budget
        self._spent_units = 0

    @property
    def spent_units(self) -> int:
        """Compute units booked so far."""
        return self._spent_units

    @property
    def remaining_units(self) -> int:
        """Compute units still available under the cap (never negative)."""
        return max(self.budget.cap_units - self._spent_units, 0)

    def authorize(self, gate: str) -> ComputeChargeDecision:
        """Decide whether running ``gate`` fits the cap — WITHOUT committing.

        Mirrors ``BudgetController.authorize_superstep``: a boundary check before
        dispatch. ``allowed=False`` when ``spent + charge > cap`` (fail-closed —
        the gate must pause/escalate, not run).
        """
        charge = self.budget.charge_for(gate)
        projected = self._spent_units + charge
        if projected > self.budget.cap_units:
            return ComputeChargeDecision(
                allowed=False,
                gate=gate,
                reason=(
                    f"gate {gate!r} charge {charge} units + spent {self._spent_units} "
                    f"= {projected} exceeds compute cap {self.budget.cap_units} units "
                    f"— pause/escalate (domains.md §3)"
                ),
                charge_units=charge,
                spent_units=self._spent_units,
                remaining_units=self.remaining_units,
            )
        return ComputeChargeDecision(
            allowed=True,
            gate=gate,
            reason="within compute cap",
            charge_units=charge,
            spent_units=self._spent_units,
            remaining_units=self.budget.cap_units - projected,
        )

    def charge(self, gate: str) -> ComputeChargeDecision:
        """Authorize AND book ``gate``'s compute charge; fail-closed on overspend.

        Raises :class:`ComputeOverspendError` (and books NOTHING) when the charge
        would cross the cap — no silent overspend. On success the spend is booked
        and the decision (with the post-charge remaining) is returned.
        """
        decision = self.authorize(gate)
        if not decision.allowed:
            raise ComputeOverspendError(decision.reason)
        self._spent_units += decision.charge_units
        return decision.model_copy(
            update={"spent_units": self._spent_units, "remaining_units": self.remaining_units}
        )
