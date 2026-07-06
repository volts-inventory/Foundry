"""`PackRegistry` — capability routing + strictest-wins hook composition.

This is the **open/closed seam**: a new domain pack is added with
``registry.register(NewPack())`` and routes by capability — NO edit to
:mod:`foundry.domains.contract`, this module, or any core graph code. The
registry is the only place that knows the set of packs, and it learns it at
runtime, never at authoring time.

Routing (domains.md §4): a pack declares the ``capabilities`` it serves; the
registry indexes packs BY capability. ``select_packs(request)`` returns every
pack whose capabilities intersect the request's ``required_capabilities`` — MULTIPLE
packs can engage for one request (a regulated ML UI product engages all three).

Composition (domains.md §4 — *a pack can only tighten*): the engaged packs' hook
results are folded through each policy's ``tighten`` join, **strictest-wins**. The
fold is seeded with the policy's IDENTITY and is order-independent (the join is
commutative + associative + idempotent), so routing is DETERMINISTIC regardless of
registration order. Before adopting each pack's contribution the registry asserts
``tighten_only``: a contribution that would ``relaxes(floor)`` the running floor is
REJECTED (:class:`~foundry.domains.contract.TightenError`) — composition can never
relax integrity, only ratchet it up.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, TypeVar

from foundry.domains.contract import (
    AIJudgePolicy,
    ApproverPolicy,
    ComputePolicy,
    DomainPackContract,
    EgressPolicy,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    StateInputPolicy,
    TightenError,
    VerdictClassPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from typing import Any


P = TypeVar("P")
T = TypeVar("T", bound="_Monotone[Any]")


class _Monotone(Protocol[P]):  # structural shape every policy satisfies
    """A policy axis: a strictest-wins ``tighten`` join + a ``relaxes`` floor probe."""

    def tighten(self, other: P, /) -> P: ...
    def relaxes(self, floor: P, /) -> bool: ...


__all__ = [
    "ComposedPolicy",
    "PackRegistry",
]


class ComposedPolicy:
    """The eight composed (strictest-wins) policies for one routed request.

    A plain immutable bundle — one composed policy per hook. Produced by
    :meth:`PackRegistry.compose`; consumed by the graph wiring (T-M5-08) to apply
    the tightened policy at the relevant seams (tiering / state_sha / gates / …).
    """

    __slots__ = (
        "ai_judge",
        "approver",
        "compute",
        "egress",
        "mode_tier",
        "perceptual_hitl",
        "state_inputs",
        "verdict_class",
    )

    def __init__(
        self,
        *,
        verdict_class: VerdictClassPolicy,
        state_inputs: StateInputPolicy,
        compute: ComputePolicy,
        mode_tier: ModeTierPolicy,
        approver: ApproverPolicy,
        egress: EgressPolicy,
        perceptual_hitl: PerceptualHITLPolicy,
        ai_judge: AIJudgePolicy | None = None,
    ) -> None:
        self.verdict_class = verdict_class
        self.state_inputs = state_inputs
        self.compute = compute
        self.mode_tier = mode_tier
        self.approver = approver
        self.egress = egress
        self.perceptual_hitl = perceptual_hitl
        self.ai_judge = ai_judge if ai_judge is not None else AIJudgePolicy.identity()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComposedPolicy):
            return NotImplemented
        return all(getattr(self, s) == getattr(other, s) for s in self.__slots__)

    def __repr__(self) -> str:
        inner = ", ".join(f"{s}={getattr(self, s)!r}" for s in self.__slots__)
        return f"ComposedPolicy({inner})"


class PackRegistry:
    """Capability-indexed registry of domain packs (the open/closed seam).

    A NEW pack is added via :meth:`register` — no core edit. Packs are kept in a
    stable registration order purely for deterministic iteration; the hook
    composition is order-INDEPENDENT (the joins are commutative/associative), so
    routing output never depends on registration order.
    """

    def __init__(self) -> None:
        # Stable insertion-ordered registry (dict preserves order; dedup by name).
        self._packs: dict[str, DomainPackContract] = {}
        # capability -> ordered pack names serving it (the routing index).
        self._by_capability: dict[str, list[str]] = {}

    # --- registration (the open/closed API) ----------------------------------

    def register(self, pack: DomainPackContract) -> None:
        """Register ``pack`` (indexed by every capability it declares).

        Idempotent by ``pack.name``: re-registering the SAME pack instance is a
        no-op; registering a DIFFERENT pack under an already-used name is a
        conflict (fail-closed — a silent replace could quietly relax a floor).
        Adding a pack requires NO edit to this module or the contract — this is
        the testable open/closed invariant.
        """
        if not isinstance(pack, DomainPackContract):
            raise TypeError(
                f"{pack!r} does not satisfy the DomainPackContract (missing hooks)"
            )
        name = pack.name
        existing = self._packs.get(name)
        if existing is not None:
            if existing is pack:
                return  # idempotent re-registration
            raise ValueError(f"a different pack is already registered under name {name!r}")
        self._packs[name] = pack
        for cap in pack.capabilities:
            self._by_capability.setdefault(cap.lower(), []).append(name)

    def packs(self) -> tuple[DomainPackContract, ...]:
        """All registered packs, in stable registration order."""
        return tuple(self._packs.values())

    # --- routing -------------------------------------------------------------

    def select_packs(
        self, request: Mapping[str, Any] | Iterable[str]
    ) -> tuple[DomainPackContract, ...]:
        """Packs engaged for ``request`` — those serving an intersecting capability.

        ``request`` may be a state-mapping view (read ``required_capabilities``) or
        a bare iterable of capability strings. Returns packs in stable
        registration order (deterministic); a pack engages iff ANY of its declared
        capabilities is in the request's capability set. Multiple packs can engage.
        """
        caps = _capabilities_of(request)
        selected: list[str] = []
        seen: set[str] = set()
        for cap in caps:
            for name in self._by_capability.get(cap, ()):
                if name not in seen:
                    seen.add(name)
                    selected.append(name)
        # Re-order by registration order for determinism independent of cap order.
        order = list(self._packs)
        selected.sort(key=order.index)
        return tuple(self._packs[name] for name in selected)

    # --- composition (strictest-wins, tighten-only) --------------------------

    def compose(self, request: Mapping[str, Any] | Iterable[str]) -> ComposedPolicy:
        """Route ``request`` then fold the engaged packs' hooks strictest-wins.

        Each of the seven policies is folded through its ``tighten`` join, seeded
        with the policy IDENTITY. Before adopting a pack's contribution the fold
        asserts ``tighten_only`` (the contribution may not ``relaxes`` the running
        floor) — composition can only ratchet integrity up, never down. The result
        is deterministic regardless of registration order.
        """
        packs = self.select_packs(request)
        view = _request_view(request)
        return ComposedPolicy(
            verdict_class=_fold(
                VerdictClassPolicy.identity(), packs, lambda p: p.assign_verdict_class(view)
            ),
            state_inputs=_fold(
                StateInputPolicy.identity(), packs, lambda p: p.state_inputs(view)
            ),
            compute=_fold(ComputePolicy.identity(), packs, lambda p: p.compute_budget(view)),
            mode_tier=_fold(
                ModeTierPolicy.identity(), packs, lambda p: p.mode_tier_policy(view)
            ),
            approver=_fold(
                ApproverPolicy.identity(), packs, lambda p: p.approver_of_record(view)
            ),
            egress=_fold(EgressPolicy.identity(), packs, lambda p: p.egress_profile(view)),
            perceptual_hitl=_fold(
                PerceptualHITLPolicy.identity(), packs, lambda p: p.perceptual_hitl(view)
            ),
            ai_judge=_fold(
                AIJudgePolicy.identity(), packs, lambda p: p.ai_judgment(view)
            ),
        )


def _fold(
    identity: T,
    packs: Sequence[DomainPackContract],
    hook: Callable[[DomainPackContract], T],
) -> T:
    """Fold one hook over ``packs`` strictest-wins, enforcing ``tighten_only``.

    The fold is two passes so the result AND the ``tighten_only`` verdict are both
    ORDER-INDEPENDENT (the join is commutative/associative/idempotent):

    1. JOIN every pack's contribution into the composed floor (strictest-wins).
    2. Verify no individual contribution ``relaxes`` that FINAL floor — i.e. no
       pack SPECIFIED a displacing value strictly weaker than the composed result.
       Checking against the final floor (not a running one) makes the raise
       independent of registration order: a STANDARD-tier contribution alongside a
       HIGH-tier one is rejected whichever registered first.

    A contribution EQUAL to the identity is a no-opinion no-op (a pack that did not
    override this hook): it can neither tighten nor relax any axis, so it is
    skipped — a pack that tightens axis A while defaulting axis B never spuriously
    trips the guard on B against a floor another pack raised on B.
    """
    contributions = [hook(pack) for pack in packs]
    floor: T = identity
    for contribution in contributions:
        if contribution != identity:
            floor = floor.tighten(contribution)
    for pack, contribution in zip(packs, contributions, strict=True):
        if contribution != identity and contribution.relaxes(floor):
            raise TightenError(
                f"pack {pack.name!r} would RELAX the composed integrity floor on "
                f"{type(floor).__name__} (a pack may only tighten, domains.md §4)"
            )
    return floor


def _capabilities_of(request: Mapping[str, Any] | Iterable[str]) -> frozenset[str]:
    """Lowercased capability set from a state-mapping view or a bare iterable."""
    if isinstance(request, Mapping):
        raw: Iterable[Any] = request.get("required_capabilities") or ()
        return frozenset(str(c).lower() for c in raw)
    return frozenset(str(c).lower() for c in request)


def _request_view(request: Mapping[str, Any] | Iterable[str]) -> Mapping[str, Any]:
    """Normalize the routing input to a mapping the hooks read (advisory view)."""
    if isinstance(request, Mapping):
        return request
    return {"required_capabilities": tuple(request)}
