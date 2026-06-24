"""The ML / data domain pack (domains.md §5 "ML / data systems", T-M5-06).

Correctness for an ML/data system is a **thresholded random variable over versioned
data** — not the deterministic behavioral oracle the core was built around. This
pack teaches the general factory that shape WITHOUT editing core: it implements the
:class:`~foundry.domains.contract.DomainPackContract` (subclassing
:class:`~foundry.domains.contract.BaseDomainPack` for the safe no-op defaults) and is
added via ``PackRegistry.register(MLDataPack())`` — the open/closed seam. It engages
on the ``{ml, data, model, training, ai}`` capabilities and TIGHTENS three hooks:

1. ``state_inputs`` (domains.md §2, ML-F2). It folds the request's declared
   data / feature / weight refs into ``state_sha`` (via the
   :func:`~foundry.domains.state_sha.ml_state_sha` formula). The model-eval verdict
   is thereby BOUND to its data + weights: a dataset / feature / relabel / weight
   change RE-KEYS the verdict so the dependency-scoped re-verification engine
   re-draws it instead of laundering a stale "already verified" PASS forward. The
   join is a pure union — declaring more refs only ever tightens the key.

2. ``assign_verdict_class`` (domains.md §1/§5, ML-F1). It marks the model-eval gate
   (:data:`MODEL_EVAL_GATE`) as **class 3 (statistical)**. Per
   :mod:`foundry.domains.verdict_class`, a class-3 verdict is grounded ONLY if it
   binds a FROZEN alpha look (the cross-fork ``(eval_set_identity, decision_id)``
   look-ledger) AND a ``state_sha`` — a class-3 verdict that cites neither is
   INVALID (fail-closed via
   :func:`~foundry.domains.verdict_class.assert_class3_binds_ledger`). The join keeps
   the stricter class per key (it never downgrades a grounding).

3. ``compute_budget`` (domains.md §3, ML-F3). Model-eval is EXPENSIVE (a retrain, a
   full eval pass consume COMPUTE — a dimension distinct from USD spend). It declares
   the model-eval/retrain gates as expensive (gated behind a cheap proxy + an explicit
   confirm-budget escalation, never auto-re-run on every fix) and a per-fix compute
   cap. The :class:`~foundry.domains.state_sha.ComputeBudgetController` fails closed on
   overspend (the gate pauses / escalates rather than silently overspending compute).

It also contributes an ``egress_profile`` HINT (no third-party trace egress for
training-data lineage). Every hook returns a MONOTONE policy: the registry composes
engaged packs strictest-wins and REJECTS any contribution that would relax the floor
(``tighten_only``) — so this pack can only ratchet integrity up, never down.

This module REUSES the existing machinery verbatim
(:func:`~foundry.domains.state_sha.ml_state_sha` for the data/weight binding,
:func:`~foundry.domains.verdict_class.assert_class3_binds_ledger` +
:mod:`foundry.state.global_records` for the alpha ledger,
:class:`~foundry.domains.state_sha.ComputeBudgetController` for the budget); it
rebuilds NONE of it, and it edits NO core module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.domains.contract import (
    BaseDomainPack,
    ComputePolicy,
    EgressPolicy,
    StateInputPolicy,
    VerdictClassPolicy,
    request_capabilities,
)
from foundry.schemas.gates import VerdictClass

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "MODEL_EVAL_GATE",
    "RETRAIN_GATE",
    "MLDataPack",
]

#: The model-eval gate key the pack assigns class 3 (statistical) and meters as an
#: expensive compute gate. A verdict on this key MUST bind a frozen alpha look + a
#: ``state_sha`` (fail-closed), and a data/weight change re-keys that ``state_sha``.
MODEL_EVAL_GATE = "model_eval"

#: The retrain gate — also expensive (it consumes COMPUTE), gated behind the proxy +
#: confirm-budget escalation alongside model-eval (domains.md §3, ML-F3).
RETRAIN_GATE = "retrain"

#: The ML capabilities this pack engages on (routing key; registry indexes by these).
_ML_CAPABILITIES: frozenset[str] = frozenset({"ml", "data", "model", "training", "ai"})

#: Default per-fix compute cap (compute units, NOT micro-USD) for the expensive ML
#: gates. A concrete deployment may tighten it further via another engaged pack — the
#: ``ComputePolicy`` join keeps the LOWER cap.
_DEFAULT_PER_FIX_COMPUTE_CAP_UNITS = 100


def _refs_from(request: Mapping[str, Any], key: str) -> frozenset[str]:
    """Lowercase-stable frozenset of the declared content-identity refs under ``key``.

    The advisory request view declares its data / feature / weight content-identity
    refs (manifest hashes / store locators) under ``data_refs`` / ``feature_defs`` /
    ``weight_refs``. We read them defensively (absent / ``None`` → empty) and as a
    frozenset so the ``StateInputPolicy`` union is order- and duplication-stable.
    """
    raw = request.get(key) or ()
    return frozenset(str(r) for r in raw)


class MLDataPack(BaseDomainPack):
    """ML/data domain pack — state_sha-keyed class-3 model-eval + a compute budget.

    Engages on ``{ml, data, model, training, ai}``. It overrides ONLY the hooks it
    tightens (``state_inputs`` / ``assign_verdict_class`` / ``compute_budget`` plus an
    ``egress_profile`` hint); the other four hooks keep the
    :class:`~foundry.domains.contract.BaseDomainPack` identity defaults, so this pack
    is the neutral element on every axis it does not touch.
    """

    name = "ml_data"
    capabilities = _ML_CAPABILITIES

    # --- Hook 2: state_sha data/weight/feature inputs (domains.md §2, ML-F2) ---
    def state_inputs(self, request: Mapping[str, Any]) -> StateInputPolicy:
        """Fold the request's data / feature / weight refs into ``state_sha``.

        Declaring these content-identity refs BINDS the model-eval verdict to its
        data + weights: the dependency-scoped re-verification engine re-keys the
        verdict (via :func:`~foundry.domains.state_sha.ml_state_sha_from_policy`)
        whenever any ref changes, so a verdict for data D / weights W can never be
        reused for D' / W' — it re-draws. The join is a pure UNION, which can only
        ever tighten the key (more verification inputs, never fewer)."""
        return StateInputPolicy(
            data_refs=_refs_from(request, "data_refs"),
            feature_defs=_refs_from(request, "feature_defs"),
            weight_refs=_refs_from(request, "weight_refs"),
        )

    # --- Hook 1: verdict-class assignment (domains.md §1/§5, ML-F1) ------------
    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        """Mark the model-eval gate class 3 (statistical) — the alpha+state_sha grounding.

        A class-3 verdict is grounded ONLY by a frozen alpha look (the cross-fork
        ``(eval_set_identity, decision_id)`` look-ledger) AND a ``state_sha`` — both
        re-checked fail-closed by
        :func:`~foundry.domains.verdict_class.assert_class3_binds_ledger`. We engage
        the assignment only when the request actually carries an ML capability (an
        empty/non-ML request gets the identity, so a misrouted request is never
        spuriously forced onto the statistical class)."""
        if not (request_capabilities(request) & _ML_CAPABILITIES):
            return VerdictClassPolicy.identity()
        return VerdictClassPolicy(classes={MODEL_EVAL_GATE: VerdictClass.STATISTICAL})

    # --- Hook 3: compute-cost budget + expensive-gate handling (§3, ML-F3) ----
    def compute_budget(self, request: Mapping[str, Any]) -> ComputePolicy:
        """Declare the model-eval / retrain gates EXPENSIVE + a per-fix compute cap.

        Model-eval and retrain consume COMPUTE (distinct from USD spend), so they are
        gated behind a cheap proxy (``require_proxy``) + an explicit confirm-budget
        escalation (``confirm_escalation``) and are NOT auto-re-run on every fix. The
        per-fix cap is metered by :class:`~foundry.domains.state_sha.ComputeBudgetController`,
        which fails closed on overspend (the gate pauses / escalates). The join keeps
        the LOWER cap, so another engaged pack can only tighten it."""
        del request  # the budget is a fixed property of the ML domain, not the request
        return ComputePolicy(
            expensive_gates=frozenset({MODEL_EVAL_GATE, RETRAIN_GATE}),
            require_proxy=True,
            confirm_escalation=True,
            per_fix_compute_cap_units=_DEFAULT_PER_FIX_COMPUTE_CAP_UNITS,
        )

    # --- Hook 6: data-egress profile HINT (domains.md §4(5)) -------------------
    def egress_profile(self, request: Mapping[str, Any]) -> EgressPolicy:
        """Forbid third-party TRACE egress for training-data lineage (a hint).

        Training-data lineage / eval traces can carry sensitive data identity, so the
        pack forbids third-party trace egress. An OR flag — once set it can only stay
        set; another pack engaging cannot re-permit it."""
        del request
        return EgressPolicy(no_third_party_trace_egress=True)
