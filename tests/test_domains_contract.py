"""DomainPackContract + PackRegistry + capability routing (T-M5-01, domains.md §4).

Contract under test:

* The seven hooks have typed signatures + SAFE DEFAULTS — a bare ``BaseDomainPack``
  engages nothing (every composed policy equals its identity).
* A pack can TIGHTEN (force Gated / veto Autonomous / add a gate-class / bind an
  approver / add a state_sha input / demand HITL) — the join takes the stricter
  side.
* The contract REFUSES a relaxing pack: a contribution that would lower the
  running integrity floor raises ``TightenError`` (composition can never relax),
  and the join itself keeps the stricter floor (a relaxing override has no effect).
* Registry: register a pack, route a request by capability to it; multiple packs
  COMPOSE strictest-wins; a NEW stub pack registers + routes WITHOUT editing
  contract/registry (the open/closed seam — a fresh pack class is defined IN THIS
  TEST and registered).
* Determinism: composition is order-independent.
"""

from __future__ import annotations

import pytest

from foundry.domains import (
    ApproverPolicy,
    BaseDomainPack,
    ComposedPolicy,
    ComputePolicy,
    DomainPackContract,
    EgressPolicy,
    ModeTierPolicy,
    PackRegistry,
    PerceptualHITLPolicy,
    StateInputPolicy,
    TightenError,
    VerdictClassPolicy,
)
from foundry.domains.contract import request_capabilities
from foundry.schemas.gates import VerdictClass
from foundry.schemas.governance import RiskTier

# --- representative packs the registry routes (mirror T-M5-05/06/07 shape) ----


class RegulatedPack(BaseDomainPack):
    """Tightens mode/tier + approver + egress (domains.md §5 Regulated)."""

    name = "regulated"
    capabilities = frozenset({"regulated", "healthcare", "fintech"})

    def mode_tier_policy(self, request: object) -> ModeTierPolicy:
        return ModeTierPolicy(
            vetoed_modes=frozenset({"autonomous", "assistant"}),
            min_tier=RiskTier.HIGH,
            force_human_release=True,
            two_person_release=True,
        )

    def approver_of_record(self, request: object) -> ApproverPolicy:
        return ApproverPolicy(
            bound_artifact_classes=frozenset(
                {"design", "test_protocol", "risk_analysis", "validation_report"}
            )
        )

    def egress_profile(self, request: object) -> EgressPolicy:
        return EgressPolicy(
            no_third_party_model_egress=True,
            no_third_party_trace_egress=True,
            no_real_phi_pan=True,
        )

    def assign_verdict_class(self, request: object) -> VerdictClassPolicy:
        return VerdictClassPolicy(classes={"validation": VerdictClass.HUMAN_WITNESSED})


class MLPack(BaseDomainPack):
    """Tightens verdict-class (statistical) + state_sha inputs + compute (domains.md §5 ML)."""

    name = "ml"
    capabilities = frozenset({"ml", "data"})

    def assign_verdict_class(self, request: object) -> VerdictClassPolicy:
        return VerdictClassPolicy(classes={"model_eval": VerdictClass.STATISTICAL})

    def state_inputs(self, request: object) -> StateInputPolicy:
        return StateInputPolicy(
            data_refs=frozenset({"ds:train"}),
            feature_defs=frozenset({"fs:v1"}),
            weight_refs=frozenset({"w:cand"}),
        )

    def compute_budget(self, request: object) -> ComputePolicy:
        return ComputePolicy(
            expensive_gates=frozenset({"model_eval"}),
            require_proxy=True,
            confirm_escalation=True,
            per_fix_compute_cap_units=5_000_000,
        )


class FrontendPack(BaseDomainPack):
    """Tightens perceptual HITL (domains.md §5 Frontend)."""

    name = "frontend"
    capabilities = frozenset({"ui", "frontend"})

    def perceptual_hitl(self, request: object) -> PerceptualHITLPolicy:
        return PerceptualHITLPolicy(
            hitl_required_capabilities=frozenset({"ui"}),
            require_rendered_evidence=True,
        )


# --- safe defaults: a bare base pack engages nothing -------------------------


def test_base_pack_satisfies_the_contract_structurally() -> None:
    pack = BaseDomainPack()
    assert isinstance(pack, DomainPackContract)


def test_seven_hooks_return_identity_on_base_pack() -> None:
    pack = BaseDomainPack()
    req: dict[str, object] = {}
    assert pack.assign_verdict_class(req) == VerdictClassPolicy.identity()
    assert pack.state_inputs(req) == StateInputPolicy.identity()
    assert pack.compute_budget(req) == ComputePolicy.identity()
    assert pack.mode_tier_policy(req) == ModeTierPolicy.identity()
    assert pack.approver_of_record(req) == ApproverPolicy.identity()
    assert pack.egress_profile(req) == EgressPolicy.identity()
    assert pack.perceptual_hitl(req) == PerceptualHITLPolicy.identity()


def test_base_pack_composes_to_identity_when_engaged() -> None:
    # A base pack with a capability engages but contributes nothing.
    class NoopPack(BaseDomainPack):
        name = "noop"
        capabilities = frozenset({"x"})

    reg = PackRegistry()
    reg.register(NoopPack())
    composed = reg.compose({"required_capabilities": ["x"]})
    assert composed == _identity_composed()


# --- the join: tighten takes the stricter side; identity is neutral ----------


def test_tighten_is_neutral_under_identity() -> None:
    p = ModeTierPolicy(vetoed_modes=frozenset({"autonomous"}), min_tier=RiskTier.HIGH)
    assert p.tighten(ModeTierPolicy.identity()) == p
    assert ModeTierPolicy.identity().tighten(p) == p


def test_tighten_keeps_stricter_tier_and_unions_vetoes() -> None:
    a = ModeTierPolicy(vetoed_modes=frozenset({"autonomous"}), min_tier=RiskTier.STANDARD)
    b = ModeTierPolicy(vetoed_modes=frozenset({"assistant"}), min_tier=RiskTier.HIGH)
    j = a.tighten(b)
    assert j.vetoed_modes == frozenset({"autonomous", "assistant"})
    assert j.min_tier is RiskTier.HIGH  # stricter tier wins
    # commutative
    assert b.tighten(a) == j


def test_tighten_verdict_class_keeps_stricter_grounding() -> None:
    weak = VerdictClassPolicy(classes={"k": VerdictClass.EXECUTION_DETERMINISTIC})
    strong = VerdictClassPolicy(classes={"k": VerdictClass.HUMAN_WITNESSED})
    assert weak.tighten(strong).classes["k"] is VerdictClass.HUMAN_WITNESSED
    assert strong.tighten(weak).classes["k"] is VerdictClass.HUMAN_WITNESSED  # never downgrades


def test_tighten_compute_keeps_lower_cap_and_unions_gates() -> None:
    a = ComputePolicy(expensive_gates=frozenset({"g1"}), per_fix_compute_cap_units=10)
    b = ComputePolicy(expensive_gates=frozenset({"g2"}), per_fix_compute_cap_units=3)
    j = a.tighten(b)
    assert j.expensive_gates == frozenset({"g1", "g2"})
    assert j.per_fix_compute_cap_units == 3  # tighter (lower) cap wins


def test_allowed_modes_drops_vetoed() -> None:
    p = ModeTierPolicy(vetoed_modes=frozenset({"autonomous", "assistant"}))
    assert p.allowed_modes(["autonomous", "assistant", "gated"]) == frozenset({"gated"})


# --- relaxes(): the integrity-floor probe ------------------------------------


def test_relaxes_detects_tier_downgrade_on_displacing_axis() -> None:
    # min_tier is the displacing axis (the join picks the stricter of two tiers):
    # a contribution SPECIFYING a strictly-lower tier than the floor relaxes.
    # LOW is the identity/no-opinion default, so it never relaxes; STANDARD < HIGH
    # is a genuine relaxation attempt.
    floor = ModeTierPolicy(min_tier=RiskTier.HIGH)
    assert ModeTierPolicy(min_tier=RiskTier.STANDARD).relaxes(floor) is True
    assert ModeTierPolicy(min_tier=RiskTier.HIGH).relaxes(floor) is False
    assert ModeTierPolicy(min_tier=RiskTier.LOW).relaxes(floor) is False  # no-opinion


def test_relaxes_detects_raised_compute_cap_and_verdict_downgrade() -> None:
    floor_c = ComputePolicy(per_fix_compute_cap_units=3)
    assert ComputePolicy(per_fix_compute_cap_units=10).relaxes(floor_c) is True  # higher cap
    assert ComputePolicy(per_fix_compute_cap_units=1).relaxes(floor_c) is False
    floor_v = VerdictClassPolicy(classes={"k": VerdictClass.HUMAN_WITNESSED})
    weaker = VerdictClassPolicy(classes={"k": VerdictClass.EXECUTION_DETERMINISTIC})
    assert weaker.relaxes(floor_v) is True  # would downgrade the grounding


def test_union_axes_never_relax_the_join_preserves_the_floor() -> None:
    # On union/OR axes a contribution contributes only its delta and can NEVER
    # relax — the join keeps every floor value (no-effect, not rejection).
    floor_e = EgressPolicy(no_real_phi_pan=True)
    assert EgressPolicy().relaxes(floor_e) is False
    assert EgressPolicy().tighten(floor_e).no_real_phi_pan is True  # floor preserved
    floor_a = ApproverPolicy(bound_artifact_classes=frozenset({"d"}))
    assert ApproverPolicy().relaxes(floor_a) is False
    assert ApproverPolicy().tighten(floor_a).bound_artifact_classes == frozenset({"d"})
    floor_si = StateInputPolicy(data_refs=frozenset({"d"}))
    assert StateInputPolicy(data_refs=frozenset({"e"})).relaxes(floor_si) is False
    assert StateInputPolicy(data_refs=frozenset({"e"})).tighten(floor_si).data_refs == frozenset(
        {"d", "e"}
    )


def test_identity_never_relaxes_anything() -> None:
    floor = ModeTierPolicy(vetoed_modes=frozenset({"autonomous"}), min_tier=RiskTier.HIGH)
    assert ModeTierPolicy.identity().relaxes(floor) is False


# --- registry: registration + routing by capability -------------------------


def test_register_routes_by_capability() -> None:
    reg = PackRegistry()
    reg.register(RegulatedPack())
    reg.register(MLPack())
    reg.register(FrontendPack())
    routed = reg.select_packs({"required_capabilities": ["healthcare"]})
    assert tuple(p.name for p in routed) == ("regulated",)
    # bare-iterable form also routes
    assert tuple(p.name for p in reg.select_packs(["ml"])) == ("ml",)
    # no matching capability -> nothing engages
    assert reg.select_packs(["unknown"]) == ()


def test_multiple_packs_engage_for_one_request() -> None:
    reg = PackRegistry()
    reg.register(RegulatedPack())
    reg.register(MLPack())
    reg.register(FrontendPack())
    routed = reg.select_packs({"required_capabilities": ["healthcare", "ml", "ui"]})
    assert {p.name for p in routed} == {"regulated", "ml", "frontend"}


def test_register_is_idempotent_but_rejects_name_conflict() -> None:
    reg = PackRegistry()
    pack = RegulatedPack()
    reg.register(pack)
    reg.register(pack)  # same instance -> no-op
    assert len(reg.packs()) == 1
    with pytest.raises(ValueError, match="already registered"):

        class Other(BaseDomainPack):
            name = "regulated"
            capabilities = frozenset({"regulated"})

        reg.register(Other())


def test_register_rejects_non_conforming_object() -> None:
    reg = PackRegistry()
    with pytest.raises(TypeError, match="DomainPackContract"):
        reg.register(object())  # type: ignore[arg-type]


# --- composition: strictest-wins across engaged packs ------------------------


def test_compose_strictest_wins_across_packs() -> None:
    reg = PackRegistry()
    reg.register(RegulatedPack())
    reg.register(MLPack())
    reg.register(FrontendPack())
    composed = reg.compose({"required_capabilities": ["healthcare", "ml", "ui"]})

    # regulated: mode/tier vetoes + approver + egress
    assert composed.mode_tier.min_tier is RiskTier.HIGH
    assert composed.mode_tier.allowed_modes(["autonomous", "assistant", "gated"]) == frozenset(
        {"gated"}
    )
    assert composed.mode_tier.force_human_release is True
    assert "validation_report" in composed.approver.bound_artifact_classes
    assert composed.egress.no_real_phi_pan is True
    # ml: statistical verdict class + state_sha inputs + expensive-gate compute
    assert composed.verdict_class.classes["model_eval"] is VerdictClass.STATISTICAL
    assert composed.state_inputs.weight_refs == frozenset({"w:cand"})
    assert composed.compute.require_proxy is True
    # frontend: perceptual HITL
    assert composed.perceptual_hitl.require_rendered_evidence is True
    assert "ui" in composed.perceptual_hitl.hitl_required_capabilities


def test_compose_is_order_independent_deterministic() -> None:
    caps = {"required_capabilities": ["healthcare", "ml", "ui"]}
    reg_a = PackRegistry()
    for p in (RegulatedPack(), MLPack(), FrontendPack()):
        reg_a.register(p)
    reg_b = PackRegistry()
    for p in (FrontendPack(), MLPack(), RegulatedPack()):  # reversed registration order
        reg_b.register(p)
    assert reg_a.compose(caps) == reg_b.compose(caps)
    # repeated compose is stable
    assert reg_a.compose(caps) == reg_a.compose(caps)


def test_compose_with_no_engaged_pack_is_identity() -> None:
    reg = PackRegistry()
    reg.register(RegulatedPack())
    assert reg.compose(["nothing-matches"]) == _identity_composed()


# --- tighten-only: a relaxing pack is REFUSED at composition ------------------


def test_relaxing_pack_is_rejected_by_compose() -> None:
    """A pack that tries to LOWER an established floor raises TightenError, so it
    cannot relax integrity even by engaging alongside a stricter pack."""

    class RelaxingPack(BaseDomainPack):
        name = "relaxer"
        capabilities = frozenset({"regulated"})  # co-engages with RegulatedPack

        def mode_tier_policy(self, request: object) -> ModeTierPolicy:
            # actively SPECIFIES a strictly-lower tier (STANDARD) than the HIGH the
            # regulated pack pinned — a genuine relaxation attempt (STANDARD is a
            # non-identity contribution, so it is NOT a no-opinion no-op).
            return ModeTierPolicy(min_tier=RiskTier.STANDARD)

    reg = PackRegistry()
    reg.register(RegulatedPack())  # establishes the strict floor first
    reg.register(RelaxingPack())
    with pytest.raises(TightenError, match="RELAX"):
        reg.compose({"required_capabilities": ["regulated"]})


def test_relaxing_override_has_no_effect_on_floor_when_alone() -> None:
    """Even alone, a relaxing contribution cannot lower the identity floor — the
    join keeps the stricter (identity) value; integrity is never relaxed."""

    class WeakeningPack(BaseDomainPack):
        name = "weak"
        capabilities = frozenset({"y"})

        def egress_profile(self, request: object) -> EgressPolicy:
            return EgressPolicy()  # all False — strictly the identity, no relaxation

    reg = PackRegistry()
    reg.register(WeakeningPack())
    composed = reg.compose(["y"])
    assert composed.egress == EgressPolicy.identity()


# --- the OPEN/CLOSED seam: add a brand-new pack with no core edit -------------


def test_open_closed_seam_new_pack_registers_and_routes_without_core_edit() -> None:
    """The key invariant: a brand-new pack class — defined HERE, never seen by
    contract.py/registry.py — registers via ``register(pack)`` and routes by
    capability, tightening policy, with ZERO edit to core."""

    class BrandNewPack(BaseDomainPack):
        name = "iot-firmware"
        capabilities = frozenset({"firmware"})

        def mode_tier_policy(self, request: object) -> ModeTierPolicy:
            return ModeTierPolicy(
                vetoed_modes=frozenset({"autonomous"}), min_tier=RiskTier.HIGH
            )

        def state_inputs(self, request: object) -> StateInputPolicy:
            return StateInputPolicy(data_refs=frozenset({"hw:rev-c"}))

    reg = PackRegistry()
    reg.register(BrandNewPack())  # one call — no contract/registry edit
    assert tuple(p.name for p in reg.select_packs(["firmware"])) == ("iot-firmware",)
    composed = reg.compose({"required_capabilities": ["firmware"]})
    assert composed.mode_tier.min_tier is RiskTier.HIGH
    assert "autonomous" not in composed.mode_tier.allowed_modes(["autonomous", "gated"])
    assert composed.state_inputs.data_refs == frozenset({"hw:rev-c"})


def test_new_pack_composes_strictest_with_existing_packs() -> None:
    """A new pack co-engaging with an existing one ratchets the floor UP only."""

    class ExtraStrictPack(BaseDomainPack):
        name = "extra"
        capabilities = frozenset({"healthcare"})

        def mode_tier_policy(self, request: object) -> ModeTierPolicy:
            return ModeTierPolicy(vetoed_modes=frozenset({"gated"}), min_tier=RiskTier.HIGH)

    reg = PackRegistry()
    reg.register(RegulatedPack())
    reg.register(ExtraStrictPack())
    composed = reg.compose(["healthcare"])
    # regulated vetoed autonomous+assistant; extra adds gated -> all three vetoed
    assert composed.mode_tier.vetoed_modes >= frozenset({"autonomous", "assistant", "gated"})


# --- request_capabilities helper --------------------------------------------


def test_request_capabilities_lowercases() -> None:
    assert request_capabilities({"required_capabilities": ["UI", "ML"]}) == frozenset(
        {"ui", "ml"}
    )
    assert request_capabilities({}) == frozenset()


# --- helpers -----------------------------------------------------------------


def _identity_composed() -> ComposedPolicy:
    return ComposedPolicy(
        verdict_class=VerdictClassPolicy.identity(),
        state_inputs=StateInputPolicy.identity(),
        compute=ComputePolicy.identity(),
        mode_tier=ModeTierPolicy.identity(),
        approver=ApproverPolicy.identity(),
        egress=EgressPolicy.identity(),
        perceptual_hitl=PerceptualHITLPolicy.identity(),
    )
