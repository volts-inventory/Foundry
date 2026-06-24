"""Tests for the FRONTEND / UX domain pack (T-M5-07, domains.md §5).

Contract under test (E7 standards):

* a UI request → a class-5 perceptual gate that forces a visual HITL and is NEVER
  auto-approved (``auto_approvable is False``); with no recorded visual human
  sign-off there is NO release (fail-closed);
* the visual-regression class-3 sub-pixel-only envelope ABSORBS a sub-pixel diff
  but FAILS a layout / affordance / locale delta (never absorbed);
* the deterministic WCAG gate is class 1 (config-evaluable) + BLOCKING;
* the pack REGISTERS + routes for a UI capability with NO core edit;
* determinism (pure hooks; stable render ``state_sha``).
"""

from __future__ import annotations

import pytest

from foundry.domains.contract import DomainPackContract, PerceptualHITLPolicy
from foundry.domains.governance_hooks import (
    mark_perceptual_hitl,
    perceptual_hitl_required,
)
from foundry.domains.packs.frontend import (
    A11Y_WCAG_GATE,
    AT_USER_OUT_OF_BAND,
    PERCEPTUAL_GATE,
    VISUAL_REGRESSION_GATE,
    FrontendPack,
    RenderDelta,
    RenderMatrixCell,
    envelope_absorbs,
    render_state_sha,
)
from foundry.domains.registry import PackRegistry
from foundry.domains.verdict_class import (
    class5_hitl_satisfied,
    gate_requirements_for,
)
from foundry.governance.sign_off import SignOffError, SignOffPoint, record_sign_off
from foundry.schemas.gates import VerdictClass
from foundry.state.seq import Seq

_UI_REQUEST = {"required_capabilities": ["ui", "web"]}
_NON_UI_REQUEST = {"required_capabilities": ["api", "data"]}
_SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="release", local=0)
_STATE_SHA = "s" * 40
_EVIDENCE = "rendered-bundle-hash"
_NONCE = "server-nonce-1"
_SUBJECT = "cas://artifact/ui-rc"


# --------------------------------------------------------------------------- #
# registration + routing (the open/closed seam — no core edit)
# --------------------------------------------------------------------------- #


def test_pack_satisfies_contract_structurally() -> None:
    assert isinstance(FrontendPack(), DomainPackContract)


def test_registers_and_routes_for_ui_capability() -> None:
    registry = PackRegistry()
    pack = FrontendPack()
    registry.register(pack)  # no core edit — the open/closed API

    selected = registry.select_packs(_UI_REQUEST)
    assert selected == (pack,)
    # routes on EVERY declared capability
    for cap in ("ui", "frontend", "ux", "web", "mobile", "visual"):
        assert registry.select_packs({"required_capabilities": [cap]}) == (pack,)


def test_does_not_route_for_non_ui_capability() -> None:
    registry = PackRegistry()
    registry.register(FrontendPack())
    assert registry.select_packs(_NON_UI_REQUEST) == ()


def test_register_is_idempotent_by_instance() -> None:
    registry = PackRegistry()
    pack = FrontendPack()
    registry.register(pack)
    registry.register(pack)  # idempotent
    assert registry.packs() == (pack,)


# --------------------------------------------------------------------------- #
# class-5 perceptual visual HITL — NEVER auto-approved (fail-closed)
# --------------------------------------------------------------------------- #


def test_perceptual_gate_is_class5_never_auto_approvable() -> None:
    policy = FrontendPack().assign_verdict_class(_UI_REQUEST)
    assert policy.classes[PERCEPTUAL_GATE] is VerdictClass.PERCEPTUAL

    req = gate_requirements_for(VerdictClass.PERCEPTUAL)
    assert req.auto_approvable is False  # taste is never delegated to a model
    assert req.requires_hitl is True
    assert req.in_signed_predicate is False  # excluded from the signed predicate


def test_perceptual_hitl_required_and_not_auto_approvable() -> None:
    composed = PackRegistry()
    composed.register(FrontendPack())
    perceptual = composed.compose(_UI_REQUEST).perceptual_hitl

    req = perceptual_hitl_required(perceptual, request=_UI_REQUEST)
    assert req.required is True
    assert req.auto_approvable is False  # UX-F1/F2 — never auto-approved
    assert req.require_rendered_evidence is True  # zone-B-signed rendered bundle


def test_no_recorded_sign_off_blocks_release_fail_closed() -> None:
    perceptual = FrontendPack().perceptual_hitl(_UI_REQUEST)
    # no recorded human visual sign-off ⇒ the gate is BLOCKED (cannot auto-clear)
    with pytest.raises(SignOffError):
        mark_perceptual_hitl(perceptual, request=_UI_REQUEST, human_sign_off=None)


def test_recorded_visual_sign_off_over_rendered_bundle_satisfies() -> None:
    perceptual = FrontendPack().perceptual_hitl(_UI_REQUEST)
    sign_off = record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash=_EVIDENCE,
        server_nonce=_NONCE,
        approver="dana@org",
        role="design-lead",
        profile="independent",
        seq=_SEQ,
        meaning_of_signing="visual sign-off over the rendered bundle",
    )
    # the recorded human sign-off clears the mandatory HITL
    cleared = mark_perceptual_hitl(
        perceptual, request=_UI_REQUEST, human_sign_off=sign_off
    )
    assert cleared is sign_off
    # and the class-5 verdict is satisfied ONLY over the EXACT bundle anchors
    assert class5_hitl_satisfied(
        sign_off,
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash=_EVIDENCE,
        server_nonce=_NONCE,
    )
    # a sign-off over a DIFFERENT bundle can never satisfy THIS gate (fail-closed)
    assert not class5_hitl_satisfied(
        sign_off,
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash="some-other-bundle",
        server_nonce=_NONCE,
    )


def test_non_ui_request_engages_no_perceptual_hitl() -> None:
    perceptual = FrontendPack().perceptual_hitl(_NON_UI_REQUEST)
    assert perceptual == PerceptualHITLPolicy.identity()
    # the no-op hook leaves a non-UI release auto-approvable on THIS axis
    assert perceptual_hitl_required(perceptual, request=_NON_UI_REQUEST).required is False


# --------------------------------------------------------------------------- #
# visual-regression class-3 — sub-pixel-only flake envelope
# --------------------------------------------------------------------------- #


def test_visual_regression_gate_is_class3_statistical() -> None:
    policy = FrontendPack().assign_verdict_class(_UI_REQUEST)
    assert policy.classes[VISUAL_REGRESSION_GATE] is VerdictClass.STATISTICAL
    req = gate_requirements_for(VerdictClass.STATISTICAL)
    assert req.requires_alpha_state_sha is True  # bound to a frozen alpha look + state_sha


def test_envelope_absorbs_sub_pixel_flake_only() -> None:
    # pure sub-pixel / font-hinting flake is absorbed — does not latch a deploy
    assert envelope_absorbs(RenderDelta(sub_pixel_diff=True)) is True


@pytest.mark.parametrize(
    "delta",
    [
        RenderDelta(sub_pixel_diff=True, layout_delta=True),
        RenderDelta(sub_pixel_diff=True, affordance_delta=True),
        RenderDelta(sub_pixel_diff=True, locale_delta=True),
        RenderDelta(layout_delta=True),
        RenderDelta(affordance_delta=True),
        RenderDelta(locale_delta=True),
    ],
)
def test_envelope_never_absorbs_layout_affordance_locale_delta(
    delta: RenderDelta,
) -> None:
    # a layout / affordance / locale delta is NEVER absorbed → it FAILS the cell
    # (routes to a class-5 perceptual-HITL re-baseline, UX-F4a) — even with a small
    # pixel diff (no "perceptual distance alone" license).
    assert envelope_absorbs(delta) is False


def test_render_state_sha_keys_on_matrix_and_baseline() -> None:
    base = "t" * 40
    cells = (
        RenderMatrixCell(screen="home", breakpoint="mobile", locale="en"),
        RenderMatrixCell(screen="home", breakpoint="mobile", locale="ar"),  # RTL locale cell
    )
    key = render_state_sha(base, cells=cells, baseline_ref="baseline-v1")

    # order-independent over the matrix (refs sorted+deduped inside state_sha)
    key_reordered = render_state_sha(
        base, cells=tuple(reversed(cells)), baseline_ref="baseline-v1"
    )
    assert key == key_reordered

    # a matrix change (drop the RTL locale cell) RE-KEYS the verdict (forces re-draw)
    key_fewer = render_state_sha(base, cells=cells[:1], baseline_ref="baseline-v1")
    assert key_fewer != key

    # a baseline change RE-KEYS too (a re-baseline re-draws the class-3 verdict)
    key_rebaselined = render_state_sha(base, cells=cells, baseline_ref="baseline-v2")
    assert key_rebaselined != key


# --------------------------------------------------------------------------- #
# dual a11y gate — deterministic WCAG BLOCKING + out-of-band AT-user step
# --------------------------------------------------------------------------- #


def test_wcag_gate_is_class1_deterministic_blocking() -> None:
    policy = FrontendPack().assign_verdict_class(_UI_REQUEST)
    assert policy.classes[A11Y_WCAG_GATE] is VerdictClass.CONFIG_EVALUABLE
    req = gate_requirements_for(VerdictClass.CONFIG_EVALUABLE)
    # deterministic machine floor: no HITL, runner-attestable, in the signed predicate
    assert req.requires_hitl is False
    assert req.auto_approvable is True
    assert req.in_signed_predicate is True


def test_at_user_step_is_out_of_band_human_dependency() -> None:
    # the screen-reader / AT-user step is named an out-of-band human dependency
    # (UX-F5 / F3) — NOT internally generable; the WCAG gate is the machine floor.
    assert "out-of-band-human" in AT_USER_OUT_OF_BAND


def test_non_ui_request_assigns_no_frontend_classes() -> None:
    policy = FrontendPack().assign_verdict_class(_NON_UI_REQUEST)
    assert policy.classes == {}


# --------------------------------------------------------------------------- #
# determinism (pure hooks; strictest-wins composition is order-independent)
# --------------------------------------------------------------------------- #


def test_hooks_are_pure_and_deterministic() -> None:
    pack = FrontendPack()
    assert pack.assign_verdict_class(_UI_REQUEST) == pack.assign_verdict_class(_UI_REQUEST)
    assert pack.perceptual_hitl(_UI_REQUEST) == pack.perceptual_hitl(_UI_REQUEST)


def test_composition_engages_all_three_gates_via_registry() -> None:
    registry = PackRegistry()
    registry.register(FrontendPack())
    composed = registry.compose(_UI_REQUEST)
    assert set(composed.verdict_class.classes) == {
        PERCEPTUAL_GATE,
        VISUAL_REGRESSION_GATE,
        A11Y_WCAG_GATE,
    }
    assert composed.perceptual_hitl.require_rendered_evidence is True
