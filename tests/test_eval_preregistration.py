"""Conformance tests for the frozen pre-registration manifest (T-M15-06).

Contract (docs/evaluation.md §2/§8; binds the alpha look-ledger in
foundry/state/global_records.py keyed on (eval_set_identity, decision_id)):

* FUNDED-RUNG GUARD: freezing a funded rung records the alpha draw + returns an
  immutable, content-addressed manifest; freezing an UNDERPOWERED rung raises
  (you cannot pre-register an underpowered study as if powered).
* NO RE-DRAW: registering the SAME (scored_eval_set_identity, decision_id) twice
  raises (the alpha budget is spent) — a fork/retrain cannot re-draw it; a
  DIFFERENT decision_id or eval_set_identity is allowed (its own budget).
* TAMPER-EVIDENT: content_identity changes if mde/alpha/rung is altered;
  assert_unmodified fails closed on a cited-identity mismatch + on a mde/alpha
  mirror that diverges from the rung.
* DETERMINISM: same inputs -> identical manifest identity.

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation.hypothesis import CentralHypothesis, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung, is_funded
from foundry.evaluation.preregistration import (
    AlphaAlreadyDrawnError,
    IncoherentPreRegistrationError,
    PreRegistration,
    TamperedPreRegistrationError,
    UnderpoweredPreRegistrationError,
    assert_unmodified,
    freeze_preregistration,
)
from foundry.state.global_records import GlobalRecords

_REPEAT_COST = 1_000  # micro-USD per task-repeat per arm
_BIG_BUDGET = 10_000_000_000  # 10k USD — never binds for the funded case
_TINY_BUDGET = 1  # cannot fund a single task-repeat per arm -> underpowered
_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8


def _pilot() -> PilotResult:
    """A small synthetic pilot summary the power calc consumes (read-only)."""
    return PilotResult(
        control_base_rate=0.2,
        effect_size_variance=0.01,
        control_resolution_variance=0.01,
        pilot_n=8,
        k_repeats=2,
        consumed_budget_microusd=10_000,
        eval_set_identity="pilot-eval-set",
    )


def _funded_rung(budget: int = _BIG_BUDGET) -> FundedPowerRung:
    return compute_power_rung(
        _pilot(),
        mde=_MDE,
        alpha=_ALPHA,
        target_power=_TARGET_POWER,
        budget_usd_microusd=budget,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )


def _hypothesis() -> CentralHypothesis:
    return CentralHypothesis(mde=_MDE, funded_power_rung=_funded_rung())


# ---------------------------------------------------------------------------
# FUNDED-RUNG GUARD + alpha draw recorded + immutable manifest
# ---------------------------------------------------------------------------


def test_freeze_funded_rung_records_alpha_and_returns_immutable_manifest() -> None:
    rung = _funded_rung()
    assert rung.funded and not rung.underpowered  # sanity: the rung IS funded
    assert is_funded(rung, _BIG_BUDGET)

    ledger = GlobalRecords()
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 0

    prereg = freeze_preregistration(
        _hypothesis(),
        rung,
        "scored-eval-set",
        decision_id="dec-1",
        ledger=ledger,
    )

    # The single alpha look was recorded against the (eval_set, decision) pair.
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 1

    # Manifest captured the committed pre-run quantities.
    assert prereg.mde == _MDE
    assert prereg.alpha == _ALPHA
    assert prereg.funded_rung == rung
    assert prereg.scored_eval_set_identity == "scored-eval-set"
    assert prereg.decision_id == "dec-1"

    # Immutable: a frozen pydantic model rejects in-place mutation.
    with pytest.raises(ValidationError):
        prereg.mde = 0.2  # type: ignore[misc]

    # Content-addressed identity is stable + non-empty.
    assert prereg.registration_identity() == prereg.content_identity()
    assert prereg.registration_identity()


def test_freeze_underpowered_rung_raises_and_does_not_draw_alpha() -> None:
    rung = _funded_rung(budget=_TINY_BUDGET)
    assert rung.underpowered and not rung.funded  # the budget cannot fund it

    ledger = GlobalRecords()
    with pytest.raises(UnderpoweredPreRegistrationError):
        freeze_preregistration(
            _hypothesis(),
            rung,
            "scored-eval-set",
            decision_id="dec-1",
            ledger=ledger,
        )
    # No alpha was drawn on the failed freeze (fail closed before recording).
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 0


# ---------------------------------------------------------------------------
# NO RE-DRAW — the fork/retrain integrity guard
# ---------------------------------------------------------------------------


def test_same_pair_registered_twice_raises_alpha_redraw_rejected() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()

    freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )

    # A fork/retrain re-presenting the SAME (eval_set_identity, decision_id) is
    # rejected — the alpha budget is already spent.
    with pytest.raises(AlphaAlreadyDrawnError):
        freeze_preregistration(
            _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
        )

    # Still exactly ONE look: the rejected re-draw never appended to the ledger.
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 1


def test_different_decision_id_is_allowed() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    # A genuinely different decision draws its own budget — allowed.
    prereg2 = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-2", ledger=ledger
    )
    assert prereg2.decision_id == "dec-2"
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 1
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-2") == 1


def test_different_eval_set_identity_is_allowed() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    # A different scored slice is a different study — its own budget, allowed.
    prereg2 = freeze_preregistration(
        _hypothesis(), rung, "other-eval-set", decision_id="dec-1", ledger=ledger
    )
    assert prereg2.scored_eval_set_identity == "other-eval-set"
    assert ledger.alpha_looks_spent("other-eval-set", "dec-1") == 1


# ---------------------------------------------------------------------------
# TAMPER-EVIDENCE — content addressing
# ---------------------------------------------------------------------------


def test_content_identity_changes_if_mde_altered() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    cited = prereg.registration_identity()

    # Post-hoc edit of the committed mde -> a DIFFERENT manifest (detectable).
    tampered = prereg.model_copy(update={"mde": 0.2})
    assert tampered.registration_identity() != cited


def test_content_identity_changes_if_alpha_altered() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    tampered = prereg.model_copy(update={"alpha": 0.2})
    assert tampered.registration_identity() != prereg.registration_identity()


def test_content_identity_changes_if_rung_altered() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    other_rung = rung.model_copy(update={"n_tasks": (rung.n_tasks or 0) + 1})
    tampered = prereg.model_copy(update={"funded_rung": other_rung})
    assert tampered.registration_identity() != prereg.registration_identity()


def test_assert_unmodified_passes_for_untouched_manifest() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    cited = prereg.registration_identity()
    # Re-derivation matches the cited identity.
    assert assert_unmodified(prereg, expected_identity=cited) == cited
    # And with no cited identity it still passes the internal-mirror invariant.
    assert assert_unmodified(prereg) == cited


def test_assert_unmodified_fails_on_cited_identity_mismatch() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    cited = prereg.registration_identity()
    # A COHERENT post-hoc edit (mde mirror + rung + embedded hypothesis all bumped
    # together): the mirror/coherence invariants pass, but the manifest no longer
    # re-derives to the originally cited identity -> caught by the identity check.
    bumped_rung = rung.model_copy(update={"mde": 0.2})
    tampered = prereg.model_copy(
        update={
            "mde": 0.2,
            "funded_rung": bumped_rung,
            "hypothesis": CentralHypothesis(mde=0.2, funded_power_rung=bumped_rung),
        }
    )
    # The tampered manifest does not re-derive to the originally cited identity.
    with pytest.raises(TamperedPreRegistrationError):
        assert_unmodified(tampered, expected_identity=cited)


def test_assert_unmodified_fails_on_mde_alpha_mirror_divergence() -> None:
    rung = _funded_rung()
    ledger = GlobalRecords()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
    )
    # Edit only the top-level mde mirror, leaving the rung's mde intact.
    tampered = prereg.model_copy(update={"mde": 0.2})
    with pytest.raises(TamperedPreRegistrationError):
        assert_unmodified(tampered)


# ---------------------------------------------------------------------------
# HYPOTHESIS <-> RUNG COHERENCE (M15-R1-01)
# ---------------------------------------------------------------------------


def _rung_with(*, mde: float = _MDE, alpha: float = _ALPHA) -> FundedPowerRung:
    return compute_power_rung(
        _pilot(),
        mde=mde,
        alpha=alpha,
        target_power=_TARGET_POWER,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )


def test_freeze_raises_when_hypothesis_mde_disagrees_with_rung() -> None:
    # The hypothesis commits a DIFFERENT mde than the funded rung being frozen.
    rung = _funded_rung()  # mde == _MDE
    hyp = CentralHypothesis(mde=0.2, funded_power_rung=_rung_with(mde=0.2))
    ledger = GlobalRecords()
    with pytest.raises(IncoherentPreRegistrationError):
        freeze_preregistration(
            hyp, rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
        )
    # Fail closed BEFORE any alpha look is recorded.
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 0


def test_freeze_raises_when_hypothesis_alpha_disagrees_with_rung() -> None:
    rung = _funded_rung()  # alpha == _ALPHA == 0.05
    # Hypothesis pins a coherent-WITH-ITSELF rung at alpha=0.01, but the rung being
    # frozen is at alpha=0.05 -> incoherent against the frozen rung.
    hyp = CentralHypothesis(
        mde=_MDE, alpha=0.01, funded_power_rung=_rung_with(alpha=0.01)
    )
    ledger = GlobalRecords()
    with pytest.raises(IncoherentPreRegistrationError):
        freeze_preregistration(
            hyp, rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
        )
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 0


def test_freeze_raises_when_hypothesis_pins_a_different_rung() -> None:
    rung = _funded_rung()
    # Same mde/alpha but a DIFFERENT (n_tasks) rung pinned on the hypothesis.
    other = rung.model_copy(update={"n_tasks": (rung.n_tasks or 0) + 1})
    hyp = CentralHypothesis(mde=_MDE, funded_power_rung=other)
    ledger = GlobalRecords()
    with pytest.raises(IncoherentPreRegistrationError):
        freeze_preregistration(
            hyp, rung, "scored-eval-set", decision_id="dec-1", ledger=ledger
        )
    assert ledger.alpha_looks_spent("scored-eval-set", "dec-1") == 0


def test_assert_unmodified_flags_an_incoherent_manifest() -> None:
    # Build a coherent manifest, then hand-build an INCOHERENT one whose mde/alpha
    # mirror still matches the rung (so the mirror check passes) but whose embedded
    # hypothesis commits a different mde. assert_unmodified must flag it.
    rung = _funded_rung()
    prereg = freeze_preregistration(
        _hypothesis(), rung, "scored-eval-set", decision_id="dec-1", ledger=GlobalRecords()
    )
    incoherent_hyp = CentralHypothesis(mde=0.2, funded_power_rung=_rung_with(mde=0.2))
    incoherent = prereg.model_copy(update={"hypothesis": incoherent_hyp})
    with pytest.raises(IncoherentPreRegistrationError):
        assert_unmodified(incoherent)


# ---------------------------------------------------------------------------
# DETERMINISM
# ---------------------------------------------------------------------------


def test_determinism_same_inputs_same_identity() -> None:
    rung = _funded_rung()
    hyp = _hypothesis()

    prereg_a = freeze_preregistration(
        hyp, rung, "scored-eval-set", decision_id="dec-1", ledger=GlobalRecords()
    )
    # Independent ledger (so the second freeze is not rejected) — the MANIFEST is
    # a pure function of its inputs, independent of which ledger recorded the draw.
    prereg_b = freeze_preregistration(
        hyp, rung, "scored-eval-set", decision_id="dec-1", ledger=GlobalRecords()
    )
    assert prereg_a.registration_identity() == prereg_b.registration_identity()
    assert prereg_a == prereg_b


def test_scored_provenance_manifest_differs_from_dry_run() -> None:
    rung = _funded_rung()
    dry = CentralHypothesis(mde=_MDE, funded_power_rung=rung)
    scored = CentralHypothesis(
        mde=_MDE, funded_power_rung=rung, provenance=Provenance.SCORED, dry_run=False
    )
    p_dry = freeze_preregistration(
        dry, rung, "scored-eval-set", decision_id="dec-1", ledger=GlobalRecords()
    )
    p_scored = freeze_preregistration(
        scored, rung, "scored-eval-set", decision_id="dec-1", ledger=GlobalRecords()
    )
    # A scored pre-registration must NOT coalesce with its dry-run rehearsal (EV4).
    assert p_dry.registration_identity() != p_scored.registration_identity()


def test_manifest_is_a_frozen_model() -> None:
    assert PreRegistration.model_config["frozen"] is True
    assert PreRegistration.model_config["extra"] == "forbid"
