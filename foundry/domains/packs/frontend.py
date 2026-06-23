"""The FRONTEND / UX domain pack (domains.md §5 "Frontend / UX / design-heavy").

Frontend correctness is *perceptual* — look / feel / interaction / copy — the one
domain where "taste is not delegated to a model" (lifecycle.md §2) becomes an
*implemented* floor, not a slogan. This pack teaches the general factory that floor
WITHOUT a core edit: it overrides the contract hooks and registers by capability
(``PackRegistry.register(FrontendPack())``), tighten-only.

Three load-bearing gates (E7 standards; domains.md UX-F1/F2/F4/F5):

1. **class-5 perceptual visual HITL** (``perceptual_hitl`` + ``assign_verdict_class``)
   — a UI release is perceptual (class 5) and is **NEVER auto-approved**. A recorded
   human visual sign-off over a **zone-B-signed rendered-evidence bundle** is
   mandatory; with no recorded sign-off there is NO release (fail-closed). Taste is
   never delegated to a model — there is no machine path that satisfies this gate.
   We REUSE :func:`foundry.domains.governance_hooks.mark_perceptual_hitl` /
   :func:`perceptual_hitl_required` (the rendered bundle rides the existing
   attestation / sign-off seam — zone-A renders, zone-B captures + signs, UX-F6).

2. **visual-regression class-3 (statistical) gate** (``assign_verdict_class``) —
   a perceptual-distance verdict against the blessed baseline across a
   device/breakpoint/locale **matrix**, keyed on a render ``state_sha`` (each matrix
   cell is a content-identity ref folded into the key, so a matrix change re-draws
   — :func:`render_state_sha`). It carries a **SUB-PIXEL-ONLY flake envelope**: the
   envelope absorbs font-hinting / sub-pixel noise so it does not latch a deploy,
   but it **NEVER absorbs a layout / affordance / locale delta** — any such delta
   FAILS the cell and routes to a class-5 perceptual-HITL re-baseline (UX-F4 / F4a).
   :func:`envelope_absorbs` is the fail-closed decider.

3. **dual a11y gate** (``assign_verdict_class``) — a deterministic, runner-signed,
   **BLOCKING** WCAG gate (class 1, config-evaluable — axe/Lighthouse rules from the
   run-immutable external config; reuses the SAST grounding) is the enforced floor;
   the **screen-reader / AT-user** step is named an **out-of-band human dependency**
   (:data:`AT_USER_OUT_OF_BAND`), not internally generable (UX-F5 / F3).

Tighten-only (taste is never auto-approved): every hook returns a monotone policy
whose join can only ratchet integrity up. The pack reuses ``verdict_class`` +
``governance_hooks`` + ``state_sha`` — it does NOT rebuild them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.domains.contract import (
    BaseDomainPack,
    PerceptualHITLPolicy,
    VerdictClassPolicy,
)
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import VerdictClass
from foundry.state.tree_sha import state_sha

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "A11Y_WCAG_GATE",
    "AT_USER_OUT_OF_BAND",
    "PERCEPTUAL_GATE",
    "VISUAL_REGRESSION_GATE",
    "FrontendPack",
    "RenderDelta",
    "RenderMatrixCell",
    "envelope_absorbs",
    "render_state_sha",
]

# --- the capabilities this pack engages on (the routing key, registry) --------
_FRONTEND_CAPABILITIES: frozenset[str] = frozenset(
    {"ui", "frontend", "ux", "web", "mobile", "visual"}
)

# --- the three gate keys this pack assigns a verdict class to -----------------
#: The mandatory class-5 perceptual visual-HITL gate (never auto-approved).
PERCEPTUAL_GATE = "frontend.perceptual"
#: The class-3 (statistical) visual-regression gate over the device/breakpoint/locale matrix.
VISUAL_REGRESSION_GATE = "frontend.visual_regression"
#: The deterministic, BLOCKING WCAG a11y gate (class 1, config-evaluable).
A11Y_WCAG_GATE = "frontend.a11y.wcag"

#: The screen-reader / AT-user a11y step is a SCHEDULED out-of-band human dependency
#: (UX-F5 / F3) — not internally generable. The pack publishes the marker; the
#: deterministic WCAG gate above is the enforced (BLOCKING) machine floor.
AT_USER_OUT_OF_BAND = "frontend.a11y.at_user:out-of-band-human"


class RenderMatrixCell(FrozenModel):
    """One cell of the visual-regression matrix — a content-identity ref.

    The visual-regression class-3 gate key is keyed over the FULL matrix
    (screens × breakpoints × states × browsers × devices × locales — domains.md
    UX-F4). Each cell contributes a stable content-identity ref to the render
    ``state_sha`` (:func:`render_state_sha`), so adding / changing ANY cell re-keys
    the verdict (forcing a re-draw, never laundering a stale PASS forward).
    """

    screen: str
    breakpoint: str = "default"
    state: str = "default"
    browser: str = "default"
    device: str = "default"
    locale: str = "default"

    def ref(self) -> str:
        """The cell's stable content-identity ref (order-independent in the key)."""
        return (
            f"cell:screen={self.screen}|bp={self.breakpoint}|state={self.state}"
            f"|browser={self.browser}|device={self.device}|locale={self.locale}"
        )


def render_state_sha(
    base_tree_sha: str,
    *,
    cells: Iterable[RenderMatrixCell],
    baseline_ref: str,
) -> str:
    """The render ``state_sha`` keying the visual-regression class-3 gate (UX-F4).

    Folds the device/breakpoint/locale **matrix** + the blessed ``baseline_ref``
    into the existing :func:`foundry.state.tree_sha.state_sha` formula (the matrix
    cells ride as content-identity ``data_refs``, the baseline as a ``feature_def``)
    — REUSING the ``state_sha`` machinery verbatim rather than rebuilding it. A
    change to ANY matrix cell or the blessed baseline yields a DIFFERENT key, so the
    cached visual-regression verdict is invalidated and re-drawn (a data/render
    change can never reuse a stale "looks the same" PASS). Pure / deterministic: the
    refs are sorted + deduplicated inside ``state_sha``, so caller ordering / the
    matrix-cell order never perturb the key.
    """
    return state_sha(
        source_tree=base_tree_sha,
        lockfile_hash="frontend.render:lockfile_in_base",
        toolchain_digest="frontend.render:toolchain_in_base",
        data_refs=tuple(c.ref() for c in cells),
        feature_defs=(f"baseline:{baseline_ref}",),
    )


class RenderDelta(FrozenModel):
    """A measured per-cell delta from the blessed baseline (the envelope's input).

    The class-3 visual-regression verdict reads this per matrix cell. The flake
    envelope absorbs ``sub_pixel_diff`` ONLY (font-hinting / sub-pixel noise); the
    three SEMANTIC axes — ``layout_delta`` / ``affordance_delta`` / ``locale_delta``
    — are NEVER absorbable (any one of them routes the cell to a class-5 perceptual
    re-baseline, UX-F4a). They default ``False`` so a pure-flake cell is the only
    envelope-eligible shape; a present semantic delta FAILS the cell (fail-closed).
    """

    sub_pixel_diff: bool = False  # font-hinting / sub-pixel noise — the ONLY absorbable axis
    layout_delta: bool = False  # NEVER absorbed → class-5 re-baseline
    affordance_delta: bool = False  # interactive-element identity / tap-target / contrast — NEVER
    locale_delta: bool = False  # RTL / text-expansion / locale-conditional render — NEVER


#: The semantic delta axes a sub-pixel-only envelope can NEVER absorb (UX-F4a). A
#: present delta on ANY of these demotes the cell from envelope to a required
#: class-5 perceptual-HITL re-baseline — the envelope is bounded to flake.
_NON_ABSORBABLE_AXES: tuple[str, ...] = ("layout_delta", "affordance_delta", "locale_delta")


def envelope_absorbs(delta: RenderDelta) -> bool:
    """Does the SUB-PIXEL-ONLY flake envelope absorb ``delta``? (UX-F4 / F4a, fail-closed).

    The envelope is bounded to font-hinting / sub-pixel flake so noise does not latch
    a deploy — it absorbs a delta ONLY when the cell's SOLE admissible deviation is
    sub-pixel. It absorbs iff:

    * a ``layout_delta`` / ``affordance_delta`` / ``locale_delta`` is present →
      **never absorbed** (returns ``False``): the delta is a real regression that
      routes to a class-5 perceptual-HITL re-baseline (taste, not a tolerance dial).

    Otherwise (no semantic delta) the residual is pure sub-pixel flake → absorbed
    (returns ``True``). A delta with NO sub-pixel diff and NO semantic delta is a
    no-op (vacuously absorbed). This is the same fail-wide stance as UX-F2a: the
    envelope never re-imports the "perceptual distance alone" failure — a present
    semantic affordance change is failed regardless of how small the pixel diff is.
    """
    # A layout / affordance / locale delta is NEVER absorbed (returns False,
    # fail-closed → class-5 re-baseline); otherwise the residual is pure sub-pixel
    # / font-hinting flake and is absorbed (returns True).
    return not any(getattr(delta, axis) for axis in _NON_ABSORBABLE_AXES)


class FrontendPack(BaseDomainPack):
    """The frontend/UX domain pack (domains.md §5) — registered by capability.

    Engages on ``{ui, frontend, ux, web, mobile, visual}``. Overrides exactly the
    two hooks it tightens — :meth:`perceptual_hitl` (the mandatory, non-auto-
    approvable visual HITL over a rendered-evidence bundle) and
    :meth:`assign_verdict_class` (class 5 perceptual / class 3 visual-regression /
    class 1 BLOCKING WCAG) — and inherits the safe no-op identity for the other
    five. Tighten-only: both overrides return a monotone policy whose join can only
    ratchet integrity up; taste is never auto-approved.
    """

    name = "frontend"
    capabilities = _FRONTEND_CAPABILITIES

    def _engaged(self, request: Mapping[str, Any]) -> bool:
        """Does ``request`` touch a frontend capability this pack engages on?"""
        from foundry.domains.contract import request_capabilities

        return bool(request_capabilities(request) & self.capabilities)

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        """Hook 7 — REQUIRE a mandatory, non-auto-approvable visual HITL (UX-F1/F2).

        For a request touching a frontend capability, the change is class-5
        perceptual: it MUST pass a recorded human visual sign-off over a rendered-
        evidence bundle and can NEVER be auto-approved. The composed policy is
        consumed by :func:`foundry.domains.governance_hooks.perceptual_hitl_required`
        / :func:`mark_perceptual_hitl` (which mandate a recorded sign-off over the
        zone-B-signed rendered bundle, fail-closed). Demanding HITL on these
        capabilities + the rendered evidence bundle only tightens.
        """
        if not self._engaged(request):
            return PerceptualHITLPolicy.identity()
        return PerceptualHITLPolicy(
            hitl_required_capabilities=self.capabilities,
            require_rendered_evidence=True,
        )

    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        """Hook 1 — assign the three frontend gate keys their verdict class (domains.md §1).

        * :data:`PERCEPTUAL_GATE` → class 5 (PERCEPTUAL) — the mandatory visual HITL,
          never auto-approved (:func:`foundry.domains.verdict_class.gate_requirements_for`
          marks class 5 ``auto_approvable=False``, ``requires_hitl=True``, excluded
          from the signed predicate).
        * :data:`VISUAL_REGRESSION_GATE` → class 3 (STATISTICAL) — the device/
          breakpoint/locale-matrix perceptual-distance gate keyed on a render
          ``state_sha`` (:func:`render_state_sha`) with the sub-pixel-only flake
          envelope (:func:`envelope_absorbs`).
        * :data:`A11Y_WCAG_GATE` → class 1 (CONFIG_EVALUABLE) — the deterministic,
          BLOCKING WCAG gate (the AT-user step is the out-of-band human dependency,
          :data:`AT_USER_OUT_OF_BAND`).

        Off a non-frontend request the hook is the no-op identity (engages nothing).
        """
        if not self._engaged(request):
            return VerdictClassPolicy.identity()
        return VerdictClassPolicy(
            classes={
                PERCEPTUAL_GATE: VerdictClass.PERCEPTUAL,  # class 5 — never auto-approved
                VISUAL_REGRESSION_GATE: VerdictClass.STATISTICAL,  # class 3 — matrix + envelope
                A11Y_WCAG_GATE: VerdictClass.CONFIG_EVALUABLE,  # class 1 — deterministic + BLOCKING
            }
        )
