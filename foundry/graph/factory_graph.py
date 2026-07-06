r"""Factory graph — compile the full Phase-1 spine + the Gated-mode emit (T-M1-14 /
T-M2-10).

(architecture.md §1/§5/§7, governance §1/§5/§6, implementation-plan §7)

This is the TOP-LEVEL graph: it mounts the supervisor (the phase-routing spine
across the role subgraphs + the deterministic gate boundaries; T-M1-13/T-M2-09)
as a single subgraph node, appends a MODE-SPECIFIC emit node, and compiles the
whole thing with the shared durable checkpointer so the run is resumable.

Topology::

    START
      -> factory     # the supervisor subgraph (mode-selected: assistant | gated)
      -> docs        # (PASS path only) terminal doc-assembly node (task [3])
      -> emit        # mode-selected emit of the PR-equivalent ExternalAction
      -> END

On a clean terminal-gate PASS the run routes ``factory -> docs -> emit``; a
halted / failed run skips ``docs`` (no writer spend, no doc artifacts) and goes
``factory -> emit``. The terminal ``docs`` node (task [3]) assembles the
company-shaped documentation set + BUILD-MANIFEST IN-GRAPH so the Technical
Writer's model spend is BUDGET-METERED through the same proxy and the authored
doc artifacts enter the CHECKPOINTED ``artifacts`` channel (rather than running
out-of-graph after ``invoke``, which spent tokens outside the ledger and never
checkpointed the doc handles). It is BEST-EFFORT — a docs failure is swallowed
and never fails the build / blocks the PR.

The ``emit`` node runs ONLY after the supervisor's phases complete. It is the
ONLY place a Phase-1/Phase-2 external EFFECT (a PR) is requested, and it does so
the way ``foundry/graph/skeleton.py`` does — by writing an :class:`ExternalAction`
to the ``actions`` channel with a DETERMINISTIC idempotency key derived via
:func:`content_digest` (NOT wall clock, L7). The outbox relay
(``foundry/durability``) is the SOLE component that ever touches the outside
world; nothing here makes a GitHub / network call. A kill-then-resume re-folds
the committed ``emit`` write and re-derives the SAME idempotency key, so
``union_actions`` (keyed by ``idempotency_key``) coalesces it — no duplicate PR
(Phase-0 exit criterion).

Two emit modes (``mode=``, threaded through to ``build_supervisor_graph``):

* ASSISTANT (default, Phase-1, FG1-3) — guard on the terminal gate PASS
  (Spec+Build+Test) and open a bare ``open_pr`` against the integrated
  FeatureDiff. No evidence pack. Back-compat: byte-for-byte the Phase-1 emit.

* GATED (Phase-2, T-M2-10) — a RELEASE that travels with its WORM evidence pack
  (governance §6). The gated emit guards on (1) the terminal gate PASS AND the
  tier-required assurance gates PASS, AND (2) ``release_allowed(...)`` — a
  recorded, approved Release sign-off bound to THIS exact decision point. Only
  then does it build a tamper-evident :class:`EvidencePack` (hash-chained,
  content-addressed, over the attested GateResults + released artifact handles +
  the recorded SignOffRecords) and emit an ``open_pr`` whose payload REFERENCES
  the pack's ``pack_uri`` (the release travels with its evidence). Fail-closed: a
  halted / failed / unsigned-release run emits NOTHING — no PR, no pack release.

PASS GUARD (governance §1/§5/§6): the assistant PR is emitted ONLY on a clean
Spec+Build+Test PASS; the gated PR additionally requires the tier's assurance
gates PASS and an admitted Release sign-off. A run that halted (a stable-FAIL
latch, an unresolved ERROR, the convergence bound spent, a blocked release) emits
NOTHING — we never open a PR for a non-released run. Both guards read only the
trust-filtered ``gates`` channel (verdicts are runner-attested; an unsigned write
never reaches here, L5), so they cannot be tricked by an unattested status-flipped
copy.

The ``add_node`` mypy seam (LEARNINGS SG2/IM5): a closure typed as the ``NodeFn``
``Callable`` alias trips ``add_node``'s overload resolution, so each emit node is
a thin concrete ``def`` (no ``cast``, no private import). The supervisor subgraph
is added as a compiled graph, which ``add_node`` accepts directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation.senior_engineer import DESIGN_KIND
from foundry.canonical import content_digest
from foundry.docs.assemble import make_documentation_node
from foundry.domains.governance_hooks import (
    perceptual_hitl_required,
    release_admitted,
    resolve_effective_mode_tier,
)
from foundry.durability.evidence_pack import build_evidence_pack
from foundry.governance.release_anchors import (
    evidence_hash,
    newest_artifact_uri,
    newest_release_sign_off,
    sign_off_nonce,
    state_sha,
)
from foundry.governance.sign_off import release_allowed
from foundry.graph.assurance_wiring import assurance_gate_set
from foundry.graph.gate_wiring import FEATURE_DIFF_KIND
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    build_supervisor_graph,
)
from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.schemas.checkpoint import CheckpointBoundary
from foundry.schemas.gates import GateStatus
from foundry.schemas.trust_root import BuildRequest
from foundry.state.factory_state import FactoryState
from foundry.trust_root.admission import FORGERY_DENY_REASONS

if TYPE_CHECKING:
    from collections.abc import MutableSequence, Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.docs.assemble import DocumentationBundle
    from foundry.domains.registry import PackRegistry
    from foundry.gates.attestation import DevGateWriteVerifier
    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact
    from foundry.schemas.gates import GateResult
    from foundry.schemas.governance import SignOffRecord
    from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

__all__ = [
    "MANDATORY_ADMISSION_FLOOR",
    "TERMINAL_PASS_GATES",
    "autonomous_pack_reachable",
    "build_factory_graph",
    "checkpoint_blocks_release",
    "gated_release_admitted",
    "pack_release_admitted",
    "terminal_gates_pass",
]

# The default deploy target an autonomous release ships to (folded into the
# deploy idempotency key; a single-target dev default).
_DEFAULT_DEPLOY_TARGET = "prod"
# Pinned hermetic build inputs for the autonomous build of the integrated
# subject. The trust root requires content-addressed (sha256:<64-hex>) inputs;
# in the dev-grade root these are stable stand-ins (real KMS/OIDC + a real
# source/base-image pin are an ops task, plan §0.3). They are CONSTANT so the
# build is a pure function of (integrated tree_sha, subject identity), hence
# resume-stable.
_AUTONOMOUS_BUILD_TYPE = "foundry-autonomous-build"
_AUTONOMOUS_SOURCE_URI = "git://foundry/integrated"
_AUTONOMOUS_SOURCE_DIGEST = "sha256:" + "5" * 64
_AUTONOMOUS_BASE_IMAGE_DIGEST = "sha256:" + "6" * 64
_AUTONOMOUS_BUILD_CONFIG_URI = "cas://build-config/autonomous"

# Supervisor subgraph node id + the emit node id.
_FACTORY = "factory"
_EMIT = "emit"
# The terminal documentation node id (task [3]). Runs on the PASS path, AFTER the
# supervisor's terminal gates and BEFORE emit, so the Technical Writer's model
# spend is budget-metered and the authored doc artifacts enter the checkpointed
# ``artifacts`` channel.
_DOCS = "docs"

# The PR is opened only when EVERY terminal gate has a PASS verdict (governance
# §1/§5): the Spec gate (product/architecture boundary) AND the Build + Test
# gates (implementation/quality boundary). The reviewer's ``review`` verdict is
# advisory here — the deterministic gates own the release bit (LEARNINGS L5).
TERMINAL_PASS_GATES: frozenset[str] = frozenset({"spec", "build", "test"})

# The MANDATORY admission floor (governance §1c/§1e/§5): the terminal gates
# (Spec+Build+Test) AND the always-on assurance integrity floor (Security+Audit)
# must be present in the admitted required set — REGARDLESS of what gate-state
# happened to land. Asserted before build/admit so admission ALONE enforces a
# floor (not merely whatever verdicts the run materialized). The tier scales
# DEPTH on top of this (supply_chain/e2e) but never removes a floor gate.
MANDATORY_ADMISSION_FLOOR: frozenset[str] = TERMINAL_PASS_GATES | frozenset({"security", "audit"})

# The release-evidence "released artifact" the pack carries + the PR opens against
# — the integrated FeatureDiff handle (a release travels with its diff).
_RELEASED_ARTIFACT_KIND = FEATURE_DIFF_KIND


def _latest_by_name(state: FactoryState) -> dict[str, Any]:
    """Newest-by-``Seq`` verdict per gate name on the (trust-filtered) channel.

    ``gates`` is admitted only for runner-attested records (``merge_gates``); pick
    the latest by ``Seq`` per name so a re-verification run's verdict (bumped
    epoch) wins over the stale record it supersedes.

    NB: this collapses by ``name`` ALONE and takes the global newest-by-``Seq``,
    whereas ``merge_gates`` keys by ``(name, run_against)``. That is deliberate
    release semantics: across a fix-loop the channel may hold same-name verdicts
    for different ``run_against`` trees; the newest integrated tree's verdict is
    the one that gates the release, so cross-``run_against`` newest-wins is correct.
    """
    latest: dict[str, Any] = {}
    for g in state.get("gates") or ():
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def _gates_all_pass(latest: dict[str, Any], names: frozenset[str] | tuple[str, ...]) -> bool:
    """True iff EVERY named gate has a newest-by-``Seq`` PASS verdict (fail-closed:
    a missing verdict or any non-PASS status is False)."""
    for name in names:
        verdict = latest.get(name)
        if verdict is None or verdict.status is not GateStatus.PASS:
            return False
    return True


def terminal_gates_pass(state: FactoryState) -> bool:
    """True iff EVERY terminal gate has a newest-by-``Seq`` PASS verdict.

    Fail-closed: a missing terminal verdict, or any non-PASS status, means the
    run did NOT complete a clean release boundary — no PR. Reads only the
    trust-filtered ``gates`` channel (verdicts are runner-attested; an unsigned
    write never reaches here, L5), so the guard cannot be tricked by an
    unattested status-flipped copy.
    """
    return _gates_all_pass(_latest_by_name(state), TERMINAL_PASS_GATES)


def checkpoint_blocks_release(state: FactoryState) -> bool:
    """True iff a RELEASE-boundary checkpoint decision HALTS the release (plan [10]).

    The checkpoint/HITL mode lets a human REJECT (or send back for changes) at the
    release stage boundary. That decision must HALT the emit even though the gates
    already PASSED — so this guard is ANDed into every emit guard (a *not blocked*
    is required to release). It reads only the steering-only ``checkpoint_decisions``
    channel.

    HARD TRUST DIRECTION (the whole point of [10]): this guard can only SUBTRACT a
    release (a human reject halts a gate-PASS run) — it can NEVER ADD one. A human
    *approve* makes ``proceeds()`` True so this returns False (does NOT block), but
    the release still requires :func:`terminal_gates_pass` /
    :func:`gated_release_admitted` to hold independently. So a checkpoint approve
    can never turn a gate FAIL into a pass: the gates remain the sole trust
    authority. Fail-closed: a recorded RELEASE decision that does not ``proceeds()``
    blocks; no decision (checkpoints off, or the boundary not reached) does not."""
    best: Any = None
    for d in state.get("checkpoint_decisions") or ():
        if d.boundary != CheckpointBoundary.RELEASE:
            continue
        if best is None or d.seq > best.seq:
            best = d
    return best is not None and not best.proceeds()


def _required_gate_names(state: FactoryState) -> frozenset[str]:
    """The full set of gate names a GATED release requires PASS — the terminal
    gates (Spec+Build+Test) PLUS the tier-scaled assurance family.

    The assurance depth is read off the ``risk_tier`` single-writer channel via
    :func:`assurance_gate_set` (gov §1e — the tier scales DEPTH, never integrity:
    the Security+Audit FLOOR is always required; STANDARD adds supply_chain; HIGH
    adds e2e). So a HIGH-tier release that skipped supply_chain/e2e can never be
    admitted by this guard."""
    assurance = assurance_gate_set(state.get("risk_tier"))
    return TERMINAL_PASS_GATES | frozenset(assurance)


def gated_release_admitted(state: FactoryState, config: RunnableConfig) -> bool:
    """True iff a GATED release may emit (fail-closed; governance §1/§5/§6).

    Two independent guards, BOTH required:

    * EVERY required gate PASS — the terminal gates (Spec+Build+Test) AND the
      tier-scaled assurance family (:func:`_required_gate_names`), newest-by-Seq
      on the trust-filtered ``gates`` channel (runner-attested only, L5);
    * ``release_allowed(...)`` — a recorded, approved Release sign-off that binds
      THIS exact decision point (subject / state_sha / evidence hash / nonce),
      re-derived against the SAME anchors the supervisor's release boundary bound
      (no drift: both import the PUBLIC anchor helpers from
      ``foundry.governance.release_anchors``). No record / not approved
      / mismatched anchors ⇒ not released.

    A halted / failed / unsigned-release run is False here → the gated emit opens
    NOTHING.
    """
    if not _gates_all_pass(_latest_by_name(state), _required_gate_names(state)):
        return False
    subject_uri = newest_artifact_uri(state, DESIGN_KIND)
    sha = state_sha(config)
    nonce = sign_off_nonce(config, "release_sign_off", subject_uri, sha)
    ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
    record = newest_release_sign_off(state)
    return release_allowed(
        record,
        subject_artifact_uri=subject_uri,
        state_sha=sha,
        presented_evidence_hash=ev_hash,
        server_nonce=nonce,
    )


# ---- domain-pack release enforcement (T-M5-08) ------------------------------
# The composed domain-pack policy is a PURE function of the request's capabilities
# + the registry, so the release boundary RE-DERIVES it here (no new state channel,
# no pack-by-pack wiring): a regulated/class-4 release requires a recorded human
# approver-of-record, a frontend/class-5 release requires the perceptual HITL, and
# the pack veto makes hands-off Autonomous UNREACHABLE for a regulated request. All
# tighten-only + fail-closed: a pack can only make the release STRICTER, never relax
# the integrity floor. The KEY invariant — adding a 4th pack is registry.register():
# this reads the composed policy generically, never pack-by-pack.


def _release_class() -> str:
    """The artifact CLASS the release-boundary approver-of-record keys on.

    The regulated pack binds the ``release`` class (and the design-control classes)
    at class 4; the release decision is keyed on ``release`` (Regulated-F2)."""
    return "release"


def pack_release_admitted(
    state: FactoryState,
    config: RunnableConfig,
    pack_registry: PackRegistry | None,
    *,
    requested_mode: str,
    ai_judge: Any = None,
) -> bool:
    """Fail-closed: does the composed domain-pack policy ADMIT this release? (T-M5-08)

    ``None`` registry → ``True`` (no packs engaged; exact prior behavior). Otherwise
    the engaged packs' composed policy is RE-DERIVED (pure function of the request's
    capabilities) and enforced at the release boundary, tighten-only:

    * **Approver-of-record (regulated / class 4).** If the policy binds the
      ``release`` artifact class, a release is admitted ONLY if a recorded, approved,
      HUMAN sign-off binds THIS subject + ``state_sha``
      (:func:`foundry.domains.governance_hooks.release_admitted`) — an
      agent-identity / Autonomous / self-judge approver is rejected (no human
      approver ⇒ no release). Two-person where the policy forces it.
    * **Perceptual HITL (frontend / class 5).** If the policy requires a perceptual
      HITL for this request's capabilities, the release is admitted ONLY if a
      recorded, approved, HUMAN visual sign-off binds the exact decision anchors —
      it is NEVER auto-approved (no machine path clears it).

    A pack can only TIGHTEN: the verdict ANDs the pack requirements onto whatever the
    base (Assistant/Gated/Autonomous) emit guard already required; it never relaxes.
    """
    if pack_registry is None:
        return True
    policy = pack_registry.compose(state)
    subject_uri = newest_artifact_uri(state, DESIGN_KIND)
    sha = state_sha(config)
    sign_offs = list(state.get("sign_offs") or ())

    # -- approver-of-record (regulated, class 4): no recorded human ⇒ no release --
    if not release_admitted(
        policy.approver,
        sign_offs,
        artifact_class=_release_class(),
        subject_artifact_uri=subject_uri,
        state_sha=sha,
        require_two_person=policy.mode_tier.two_person_release,
    ):
        return False

    # -- perceptual HITL (frontend, class 5): never auto-approved ----------------
    hitl = perceptual_hitl_required(policy.perceptual_hitl, request=state)
    if hitl.required:
        nonce = sign_off_nonce(config, "release_sign_off", subject_uri, sha)
        ev_hash = evidence_hash(subject_uri, sha, list(state.get("gates") or ()))
        if not _perceptual_hitl_satisfied(
            sign_offs,
            subject_artifact_uri=subject_uri,
            state_sha=sha,
            presented_evidence_hash=ev_hash,
            server_nonce=nonce,
        ):
            return False

    # -- AI-expert judgment (autonomous): the machine stand-in for the human ----
    # sign-offs. In a declared-autonomous run the packs contribute per-domain
    # RUBRICS instead of human sign-off demands; each is judged through the
    # proxy's TRUSTED judgment path and EVERY domain must approve — fail-closed
    # (no judge wired / judge error / refusal ⇒ no release).
    if policy.ai_judge.required:
        if ai_judge is None:
            return False
        if not ai_judge(state, config, dict(policy.ai_judge.rubrics)):
            return False
    return True


_AI_JUDGE_SOP = (
    "You are the release judge for a hands-off (autonomous) software factory. "
    "You are given a DOMAIN RUBRIC and the release evidence (the request, gate "
    "verdicts, artifact inventory). Judge STRICTLY against the rubric; when the "
    "evidence is insufficient to satisfy it, refuse (fail-closed). Reply with "
    "JSON only: status 'approve' or 'request-changes', plus findings."
)


def _ai_judge_evidence(state: FactoryState) -> str:
    """A compact, model-readable release-evidence digest for the AI judge."""
    lines = [f"REQUEST: {str(state.get('request') or '')[:800]}"]
    gates = list(state.get("gates") or ())
    if gates:
        lines.append("GATES:")
        for g in gates[-24:]:
            name = getattr(g, "name", None) or (g.get("name") if isinstance(g, dict) else "?")
            status = getattr(g, "status", None) or (g.get("status") if isinstance(g, dict) else "?")
            lines.append(f"- {name}: {status}")
    arts = list(state.get("artifacts") or ())
    if arts:
        kinds: dict[str, int] = {}
        for a in arts:
            kind = str(getattr(a, "kind", None) or (a.get("kind") if isinstance(a, dict) else "?"))
            kinds[kind] = kinds.get(kind, 0) + 1
        lines.append("ARTIFACTS: " + ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items())))
    return "\n".join(lines)


def make_pack_ai_judge(rt: AgentRuntime) -> Any:
    """Build the release AI judge over the proxy's TRUSTED judgment path.

    Each (state_sha, domain) verdict is judged ONCE and memoized — the emit
    guards may re-evaluate, and a judgment must not be re-billed or flip between
    evaluations of the same release. The verdict is a proxy-stamped GateResult
    (grammar-constrained status, signed binding to subject + state_sha), so a
    compromised adapter cannot forge an approval — the same anti-spoofing the
    human sign-off path has."""
    verdicts: dict[tuple[str, str], bool] = {}

    def judge(state: FactoryState, config: RunnableConfig, rubrics: dict[str, str]) -> bool:
        subject = newest_artifact_uri(state, DESIGN_KIND) or "artifact://release"
        sha = state_sha(config)
        for domain in sorted(rubrics):
            key = (sha, domain)
            if key not in verdicts:
                nonce = sign_off_nonce(config, f"ai_judge_{domain}", subject, sha)
                seq = rt.seq(config, "release_ai_judge", f"judge:{domain}")
                try:
                    res = rt.proxy.judge(
                        "release_ai_judge",
                        subject,
                        sha,
                        nonce,
                        _AI_JUDGE_SOP,
                        [{
                            "role": "user",
                            "content": (
                                f"DOMAIN: {domain}\nRUBRIC:\n{rubrics[domain]}\n\n"
                                f"RELEASE EVIDENCE:\n{_ai_judge_evidence(state)}\n\n"
                                "Judge this release against the rubric."
                            ),
                        }],
                        seq=seq,
                        tier="T1",
                        gate_name=f"ai_judge_{domain}",
                    )
                    verdicts[key] = res.gate_result.status is GateStatus.PASS
                    findings = "; ".join(res.gate_result.findings[:3])
                except Exception as exc:  # fail-closed: an unjudgeable release is refused
                    verdicts[key] = False
                    findings = f"judge error: {exc}"
                print(f"[ai-judge] {domain}: "
                      f"{'APPROVED' if verdicts[key] else 'REFUSED'}"
                      f"{' — ' + findings if findings else ''}")
            if not verdicts[key]:
                return False
        return True

    return judge


def _perceptual_hitl_satisfied(
    sign_offs: list[SignOffRecord],
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
) -> bool:
    """A class-5 perceptual gate is satisfied ONLY by a recorded, approved HUMAN
    visual sign-off binding the exact decision anchors (UX-F1/F2, fail-closed).

    Reuses :func:`foundry.domains.verdict_class.class5_hitl_satisfied` over the
    newest matching record; a self-judge / agent / autonomous approver can never
    satisfy it (taste is never delegated to a model — never auto-approved)."""
    from foundry.domains.governance_hooks import _is_human_approver
    from foundry.domains.verdict_class import class5_hitl_satisfied

    for record in sign_offs:
        if not _is_human_approver(record.approver):
            continue
        if class5_hitl_satisfied(
            record,
            subject_artifact_uri=subject_artifact_uri,
            state_sha=state_sha,
            presented_evidence_hash=presented_evidence_hash,
            server_nonce=server_nonce,
        ):
            return True
    return False


def autonomous_pack_reachable(
    state: FactoryState,
    pack_registry: PackRegistry | None,
    *,
    requested_mode: str,
    seq: Any,
) -> bool:
    """Is hands-off Autonomous still REACHABLE under the composed pack policy?

    ``None`` registry → ``True`` (no veto). Otherwise the composed ``mode_tier``
    policy's veto is applied via :func:`resolve_effective_mode_tier`: a regulated
    request VETOES Autonomous, so this returns ``False`` and the autonomous emit
    must fall back to the recorded-human approver-of-record path (it can NEVER
    hands-off deploy a regulated artifact). Pure/deterministic."""
    if pack_registry is None:
        return True
    policy = pack_registry.compose(state)
    eff = resolve_effective_mode_tier(
        policy.mode_tier,
        requested_mode=requested_mode,
        request=state,
        capabilities=state.get("required_capabilities"),
        seq=seq,
    )
    return eff.autonomous_reachable


def _pr_subject(state: FactoryState) -> Artifact | None:
    """The integrated FeatureDiff handle the PR opens against (newest-by-``Seq``).

    ``artifacts`` folds by ``merge_by_uri`` (insertion order, not ``Seq``), so we
    pick the ``feature_diff`` handle with the max ``Seq`` deterministically
    (mirrors ``gate_wiring`` / ``reviewer_critic`` newest-by-Seq selection) —
    NOT ``artifacts[-1]``.
    """
    subject: Artifact | None = None
    for a in state.get("artifacts") or ():
        if a.kind != FEATURE_DIFF_KIND:
            continue
        if subject is None or a.seq > subject.seq:
            subject = a
    return subject


def _attested_terminal_results(state: FactoryState) -> tuple[GateResult, ...]:
    """The newest-by-``Seq`` attested GateResult for each required release gate,
    ordered by gate name (a stable, deterministic order so the evidence pack's
    chain head is reproducible across resume — L7).

    These are the verdicts the release is admitted on — exactly the ones the
    evidence pack must chain over (governance §4/§6: the WORM-anchored attested
    verdicts)."""
    latest = _latest_by_name(state)
    required = _required_gate_names(state)
    return tuple(
        latest[name] for name in sorted(required) if name in latest
    )


def _attested_run_against(gate_results: tuple[GateResult, ...]) -> str | None:
    """The actual materialized ``tree_sha`` the gates ran against — read off one of
    the attested terminal/assurance GateResults the pack folds over (every
    GateResult carries the TRUE ``run_against`` of the integrated tree it was
    evaluated on).

    Binding the pack's ``run_against`` to an attested verdict's ``run_against``
    (rather than a config-supplied ``state_sha`` LABEL) makes the pack's stated
    tree HONEST — it is the exact tree the gates were actually run over (M2-R1-02).
    The required release gates all share ONE integrated ``tree_sha`` (GW2), so any
    folded-over verdict gives the same value; ``None`` only when no verdict exists
    (admission already fail-closed before this is reached)."""
    for g in gate_results:
        if g.run_against:
            return g.run_against
    return None


def _scanner_config_rev(gate_results: tuple[GateResult, ...]) -> str | None:
    """The pinned scanner-config revision the release was assured under — read off
    an attested verdict (the runner stamps ``scanner_config_rev`` on every
    GateResult; AS4), NOT the runner's private attribute. ``None`` when no attested
    verdict carries one (e.g. judgment-only) — the pack records it as absent."""
    for g in gate_results:
        if g.scanner_config_rev is not None:
            return g.scanner_config_rev
    return None


def _released_artifacts(state: FactoryState) -> tuple[Artifact, ...]:
    """The released artifact handles the evidence pack carries — the integrated
    FeatureDiff (newest-by-Seq). A pure function of state so the pack is stable on
    resume."""
    subject = _pr_subject(state)
    return (subject,) if subject is not None else ()


def _release_sign_offs(state: FactoryState) -> tuple[SignOffRecord, ...]:
    """The recorded Release SignOffRecords the evidence pack chains over (ordered
    by ``Seq`` for a deterministic chain head). The release-approver record is the
    accountable approver-of-record the pack binds (gov §6); chaining over its
    content identity makes a swapped / dropped sign-off tamper-evident."""
    records = [s for s in (state.get("sign_offs") or ()) if s.role == "release-approver"]
    records.sort(key=lambda s: s.seq)
    return tuple(records)


# ---- Autonomous mode (T-M4-10) ----------------------------------------------
# Autonomous mode runs the SAME trusted Gated assurance spine, but the RELEASE
# boundary is the TRUST ROOT (build → admit → deploy, hands-off): there is NO
# human/recorded sign-off gate. ADMISSION is the SOLE release gate — a DENY
# emits NOTHING (fail-closed); a halted/failed/denied run deploys NOTHING.


def _autonomous_attested_gates(state: FactoryState) -> tuple[GateResult, ...]:
    """The newest-by-``Seq`` attested GateResult per required release gate — the
    trust-filtered, runner-attested gate-state admission re-verifies. Reuses
    :func:`_attested_terminal_results` (identical selection)."""
    return _attested_terminal_results(state)


def _admission_tree_and_gates(
    gate_results: tuple[GateResult, ...],
) -> tuple[str, tuple[GateResult, ...], dict[str, str]] | None:
    """The integrated provenance ``tree_sha``, the FULL attested gate-state across
    BOTH trees, and the per-gate name→tree mapping admission re-verifies against.

    Across the gated spine the terminal gates (Spec/Build/Test) and the assurance
    gates (Security/Audit/...) run on DISTINCT materialized trees (the assurance
    materializer RE-HASHES the augmented tree, AW2), so the gate-state spans
    >1 ``run_against``. The autonomous build is keyed to the SINGLE ``run_against``
    group covering the MOST attested gates — the integrated tree the largest
    coherent gate-state is keyed to (the assurance group, which always carries the
    Security+Audit floor) — so the provenance binds that tree.

    But admission must CRYPTOGRAPHICALLY re-verify ALL required gates, not just the
    assurance group (M4-R1-01): the terminal Spec/Build/Test gates ran on the
    terminal tree, so we hand admission EVERY attested gate plus a per-gate
    name→tree mapping pinning each to ITS actual ``run_against`` (terminal gates to
    the terminal tree, assurance gates to the provenance tree). Admission's
    input-bound check then covers the terminal gates against their own tree — no
    cross-tree laundering, and the terminal gates no longer rest only on the emit
    guard's fail-closed ``merge_gates`` filter.

    Returns ``None`` when no attested verdict carries a ``run_against`` (the emit
    then fail-closes — deploy nothing)."""
    groups: dict[str, list[GateResult]] = {}
    for g in gate_results:
        if g.run_against:
            groups.setdefault(g.run_against, []).append(g)
    if not groups:
        return None
    # Largest group wins as the provenance/build tree; ``run_against`` tiebreak
    # keeps the choice deterministic (resume-stable) when two trees tie.
    tree_sha = max(groups, key=lambda t: (len(groups[t]), t))
    # The FULL gate-state (every attested verdict carrying a run_against), ordered
    # by (name, tree) for a deterministic, resume-stable tuple, plus the per-gate
    # expected tree. A gate bound to the provenance tree maps to it; a terminal
    # gate on the terminal tree maps to its own — admission re-verifies each
    # against its tree. The expected tree per name is the NEWEST-by-Seq verdict's
    # ``run_against`` (matching admission's own ``_latest_by_name`` selection), so
    # a fix-loop's superseding verdict pins the tree admission actually checks.
    all_bound = tuple(
        sorted((g for gs in groups.values() for g in gs), key=lambda g: (g.name, g.run_against))
    )
    newest: dict[str, GateResult] = {}
    for g in all_bound:
        cur = newest.get(g.name)
        if cur is None or g.seq > cur.seq:
            newest[g.name] = g
    expected_trees = {name: g.run_against for name, g in newest.items()}
    return tree_sha, all_bound, expected_trees


def _autonomous_build_request(state: FactoryState, tree_sha: str, seq: Any) -> BuildRequest:
    """The pinned :class:`BuildRequest` for the integrated subject (hands-off).

    A PURE function of the integrated ``tree_sha`` + the constant pinned dev
    inputs, so the produced ``subject_digest`` (and thus the whole build → admit
    → deploy chain) is resume-stable (L7). Every input is content-addressed
    (sha256:<hex>) so the hermetic builder accepts it; the integrated tree is
    the ``tree_sha`` the assurance gate-state is keyed to, so the resulting
    provenance binds the gates admission re-verifies."""
    return BuildRequest(
        build_type=_AUTONOMOUS_BUILD_TYPE,
        source_uri=_AUTONOMOUS_SOURCE_URI,
        source_digest=_AUTONOMOUS_SOURCE_DIGEST,
        tree_sha=tree_sha,
        base_image_digest=_AUTONOMOUS_BASE_IMAGE_DIGEST,
        build_config_uri=_AUTONOMOUS_BUILD_CONFIG_URI,
        hermetic=True,
        seq=seq,
    )


def build_factory_graph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str,
    checkpointer: BaseCheckpointSaver | None = None,
    index: SymbolIndex | None = None,
    max_fix_attempts: int = 3,
    mode: str = MODE_ASSISTANT,
    builder: TrustedBuilder | None = None,
    admission: AdmissionController | None = None,
    deployer: Deployer | None = None,
    gate_verifier: DevGateWriteVerifier | None = None,
    discriminator_factory: DiscriminatorFactory | None = None,
    profile: VerificationProfile | None = None,
    pack_registry: PackRegistry | None = None,
    doc_sink: MutableSequence[DocumentationBundle] | None = None,
    checkpoints: bool | Sequence[str | CheckpointBoundary] | None = None,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
) -> CompiledStateGraph[FactoryState]:
    """Compile the full factory graph (Assistant-mode default; Gated/Autonomous opt-in).

    Mounts :func:`build_supervisor_graph` (threaded the SAME ``mode``) as the
    ``factory`` subgraph node, then a MODE-SPECIFIC ``emit`` node, then ``END``
    (``START -> factory -> emit -> END``), and compiles with the injected
    ``checkpointer`` so the run is durable + resumable.

    Parameters
    ----------
    rt:
        The shared :class:`AgentRuntime` (one ``EpochCounter`` + one model egress;
        AR1/L6) threaded into every layer subgraph, the gate nodes, AND the emit
        node's ``Seq`` minting. Its ``adapter`` must be set (the implementation
        layer's Junior requires it).
    runner:
        The :class:`TwoZoneGateRunner` the gate boundaries attest verdicts through
        (verdicts come ONLY from it; L5/GW2).
    resolve / resolve_blob:
        The two materialization seams (GW1a/D-R1-02), forwarded to the supervisor.
    source_dir:
        Base tree cloned into each per-task sandbox (implementation layer; INFRA).
    checkpointer:
        The durable saver attached at compile so the graph is resumable; a
        kill-then-resume re-folds committed writes (including the ``emit``
        ExternalAction) and the keyed ``union_actions`` reducer coalesces the
        re-emitted PR (same idempotency key) — no duplicate PR. ``None`` compiles
        a non-checkpointed graph (callers normally pass a
        :class:`FoundryCheckpointer.saver` or a dev ``MemorySaver``).
    index:
        Context retriever for the Principal Architect (``None`` = greenfield).
    max_fix_attempts:
        Convergence bound forwarded to the supervisor's gate routing.
    mode:
        ``"assistant"`` (DEFAULT) compiles the Phase-1 spine + the bare-PR emit
        (back-compat). ``"gated"`` threads the Gated supervisor (tiering +
        design/release sign-off + assurance) and the evidence-pack-carrying,
        release-gated emit. ``"autonomous"`` (Phase-4, T-M4-10) threads the SAME
        assurance spine but the emit is the hands-off TRUST-ROOT chain
        (build → admit → deploy): admission is the SOLE release gate (no human
        / recorded sign-off), and a DENY / halted run deploys NOTHING
        (fail-closed).
    builder / admission / deployer / gate_verifier:
        The Phase-4 trust-root components the AUTONOMOUS emit wires hands-off
        (injected so tests can pass doubles; INFRA): the hermetic
        :class:`TrustedBuilder`, the fail-closed deploy-time
        :class:`AdmissionController` (admission is the SOLE release gate), the
        least-privileged :class:`Deployer`, and the :class:`DevGateWriteVerifier`
        the controller re-verifies the build + gate signatures under (it shares
        the runner's zone-B key + the runner's cross-fork records). Required for
        ``mode="autonomous"``; ignored otherwise.
    discriminator_factory:
        OPTIONAL malicious-test defense (A-M4-01), threaded straight through to
        :func:`build_supervisor_graph` and on to the ``build_test_gate`` node's
        :func:`run_build_test_gates`. When provided, the COMPILED Test gate in the
        Gated/Autonomous (and Assistant) spine CONSULTS the held-out-mutant +
        behavioral-invariance discriminator the factory produces — so a gamed-green
        suite that clears the adequacy floor but does NOT kill a held-out mutant is
        FAILed by the SHIPPING graph (the attested FAIL then halts the autonomous
        emit guard → deploy NOTHING), not only in ``tests/test_phase4_exit.py``.
        The held-out mutant SET is a dev-grade CURATED set (a real mutation engine
        is heavier; plan §0.3). ``None`` (the default) keeps the adequacy-floor-only
        behavior byte-for-byte (back-compat for every existing caller).
    pack_registry:
        OPTIONAL domain-pack registry (T-M5-08), threaded to the supervisor (engages
        the packs at triage) AND read by the emit boundary to enforce the composed
        policy fail-closed: a regulated/class-4 release requires a recorded human
        approver-of-record (an Autonomous emit can NOT hands-off deploy a regulated
        artifact — the veto makes Autonomous unreachable), a frontend/class-5 release
        requires the perceptual HITL (never auto-approved). Tighten-only: packs can
        only make the release STRICTER, never relax the integrity floor. ``None`` (the
        DEFAULT) engages NO packs → exact prior behavior (back-compat). The KEY
        invariant: adding a 4th pack is ``registry.register(pack)`` — this wiring
        reads the COMPOSED policy generically, never pack-by-pack.
    doc_sink:
        OPTIONAL in-process holder (task [3]) the terminal ``docs`` node appends the
        assembled :class:`DocumentationBundle` to, so an in-process caller (the CLI)
        can write the ALREADY-assembled tree to disk without re-authoring (no second
        round of model spend). It is a plain in-memory list — NOT a state channel —
        so it never rides the checkpoint; the durable record of the authored docs is
        the doc artifacts the node emits onto the (checkpointed) ``artifacts``
        channel. ``None`` (the DEFAULT) still runs the docs node (its writer spend is
        budget-metered + its artifacts checkpointed) but discards the rendered bundle.
    checkpoints:
        OPTIONAL checkpoint/HITL mode (plan [10]), threaded straight to
        :func:`build_supervisor_graph`. ``None``/``False`` (the DEFAULT) inserts NO
        pause nodes — a non-checkpoint run is byte-identical to today (back-compat).
        ``True`` pauses at all three stage boundaries; a sequence of boundary names
        pauses at exactly those. At each pause the supervisor surfaces a READ-ONLY
        inspection payload via LangGraph's ``interrupt()`` (resumable on the
        ``checkpointer`` above) and awaits a human :class:`CheckpointDecision` fed
        back via ``Command(resume=...)``. **Trust:** a checkpoint *approve* only
        lets the run PROCEED to the (unchanged) gate boundary — the emit guards
        below still require :func:`terminal_gates_pass` /
        :func:`gated_release_admitted`, so a checkpoint approve can NEVER turn a
        gate FAIL into a release. A RELEASE-boundary *reject* additionally HALTS the
        emit via :func:`checkpoint_blocks_release` (it can only subtract a release,
        never add one). The Design/Release sign-off is unchanged.

    Returns the COMPILED top-level graph.
    """
    gated = mode == MODE_GATED
    autonomous = mode == MODE_AUTONOMOUS
    if autonomous and (
        builder is None or admission is None or deployer is None or gate_verifier is None
    ):
        raise ValueError(
            "autonomous mode requires builder, admission, deployer, and "
            "gate_verifier (the trust-root chain build → admit → deploy)"
        )
    # The AI release judge (hook 8): in a declared-autonomous run the packs'
    # human-judgment slots are filled by an AI expert through the proxy's trusted
    # judgment path instead of being skipped. Built once per compiled graph so
    # its per-(state_sha, domain) verdict memo spans the emit guards.
    pack_ai_judge = make_pack_ai_judge(rt) if pack_registry is not None else None

    supervisor = build_supervisor_graph(
        rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        source_dir=source_dir,
        index=index,
        max_fix_attempts=max_fix_attempts,
        mode=mode,
        discriminator_factory=discriminator_factory,
        profile=profile,
        pack_registry=pack_registry,
        checkpoints=checkpoints,
        max_slots=max_slots,
        cost_cap_microusd=cost_cap_microusd,
    ).compile()

    def emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Assistant-mode emit: request the bare PR-equivalent external effect.

        Runs AFTER the supervisor's phases complete. Guards on the terminal gate
        PASS (governance §1/§5): only on a clean Spec+Build+Test PASS do we emit
        the ``open_pr`` ExternalAction; a halted/failed run emits NOTHING (no PR).

        The idempotency key is DETERMINISTIC — ``content_digest`` over
        (``run_id``, the integrated artifact's identity) — so a kill-then-resume
        re-derives the SAME key (NOT a wall-clock value, L7) and the keyed
        ``union_actions`` reducer coalesces the re-emission. No real GitHub/network
        call happens here: the action is a request on the ``actions`` channel; the
        outbox relay is the sole component that touches the outside world.
        """
        if not terminal_gates_pass(state):
            # Halted / failed run: do NOT open a PR. Mark the phase only.
            return {"phase": "halted"}
        if checkpoint_blocks_release(state):
            # A human REJECTED at the release checkpoint (plan [10]) — HALT even
            # though the gates passed. This can only SUBTRACT a release; it can
            # never make a FAILing gate pass (the terminal-gate guard above is
            # independent and unchanged).
            return {"phase": "halted"}
        if not pack_release_admitted(state, config, pack_registry, requested_mode=mode,
                                      ai_judge=pack_ai_judge):
            # An engaged domain pack TIGHTENS the release (a recorded human
            # approver-of-record / perceptual HITL is required) and is unmet —
            # fail-closed: open NOTHING (tighten-only; packs never relax).
            return {"phase": "halted"}
        subject = _pr_subject(state)
        if subject is None:
            # No reviewed artifact to open a PR against — fail-closed (no PR).
            return {"phase": "halted"}

        run_id = config["configurable"]["thread_id"]
        # Process-stable key derived from run + artifact identity (L7 — never
        # hash()/wall clock), so resume re-emits the SAME key and dedups.
        key = content_digest({"run_id": run_id, "artifact_uri": subject.uri})
        action = ExternalAction(
            idempotency_key=f"open_pr:{key}",
            kind="open_pr",
            payload_uri=subject.uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            seq=rt.seq(config, _EMIT, "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    def gated_emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Gated-mode emit: a RELEASE that travels with its WORM evidence pack.

        Runs AFTER the Gated supervisor completes. Fail-closed on
        :func:`gated_release_admitted` — every required gate (terminal +
        tier-scaled assurance) PASS AND an admitted Release sign-off
        (``release_allowed``). A halted (vulnerable-change latch / bound spent /
        blocked release) run emits NOTHING — no PR, no evidence-pack release
        (governance §1/§5/§6).

        On admission it builds a tamper-evident :class:`EvidencePack`
        (:func:`build_evidence_pack`) over the attested GateResults (the WORM-
        anchored verdicts the release was admitted on), the released artifact
        handles (the integrated FeatureDiff), and the recorded Release
        SignOffRecords — hash-chained + content-addressed. The ``open_pr`` action
        then carries the pack's ``pack_uri`` as its payload, so the release
        TRAVELS WITH its evidence (gov §6). No GitHub/network call: the action is
        a request on the ``actions`` channel.

        The idempotency key folds in the ``pack_uri`` (which is a pure function of
        the attested members, L7) alongside run + subject identity, so resume
        re-derives the SAME key and ``union_actions`` coalesces it — no duplicate
        release PR.
        """
        if not gated_release_admitted(state, config):
            # Not released (halt / vulnerable change / unsigned release): emit
            # NOTHING — no PR, no evidence-pack release.
            return {"phase": "halted"}
        if checkpoint_blocks_release(state):
            # A human REJECTED at the release checkpoint (plan [10]) — HALT even
            # though the gates + Release sign-off admitted. Subtractive only: it
            # can never turn a gate FAIL into a release (gated_release_admitted is
            # independent and unchanged).
            return {"phase": "halted"}
        if not pack_release_admitted(state, config, pack_registry, requested_mode=mode,
                                      ai_judge=pack_ai_judge):
            # An engaged domain pack TIGHTENS the release beyond the self-judge
            # Release sign-off (a recorded HUMAN approver-of-record / perceptual
            # HITL is required) and is unmet — fail-closed: no PR, no pack release.
            return {"phase": "halted"}
        subject = _pr_subject(state)
        if subject is None:
            return {"phase": "halted"}

        gate_results = _attested_terminal_results(state)
        artifacts = _released_artifacts(state)
        sign_offs = _release_sign_offs(state)
        # The pack's ``run_against`` is the ACTUAL materialized tree_sha the gates
        # ran against — read off one of the attested verdicts the pack folds over
        # (they carry the true integrated tree_sha; GW2 binds them all to ONE tree),
        # NOT a config-supplied state_sha LABEL. So the pack's stated tree is HONEST:
        # it matches exactly what was gated (M2-R1-02). Fall back to the config
        # state_sha only if (impossibly, post-admission) no verdict carries one.
        run_against = _attested_run_against(gate_results) or state_sha(config)
        pack = build_evidence_pack(
            run_against=run_against,
            gate_results=gate_results,
            artifacts=artifacts,
            sign_offs=sign_offs,
            # The pinned scanner-config revision the release was assured under is
            # READ off an attested verdict (the runner stamps it on every
            # GateResult; AS4) — never the runner's private attribute.
            scanner_config_rev=_scanner_config_rev(gate_results),
            seq=rt.seq(config, _EMIT, "evidence_pack:release"),
        )

        run_id = config["configurable"]["thread_id"]
        # Process-stable key (L7): run + the (content-addressed) subject identity
        # ONLY — the SAME inputs the assistant key uses, so resume re-derives the
        # SAME key and union_actions coalesces the release PR. The pack_uri is
        # DELIBERATELY excluded from the key: it folds in the gate results'
        # content-identities, whose Seq is bumped on a resume re-attestation, so a
        # pack_uri-keyed action would NOT coalesce (it would duplicate the PR).
        # The pack still TRAVELS as the payload; the key only needs to be stable.
        key = content_digest({"run_id": run_id, "artifact_uri": subject.uri})
        action = ExternalAction(
            idempotency_key=f"open_pr:{key}",
            kind="open_pr",
            # The release travels WITH its evidence: the PR payload is the
            # content-addressed evidence-pack handle (gov §6).
            payload_uri=pack.pack_uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            gate_bearing=True,  # a release PR is gate-bearing (never path-3 dedup)
            seq=rt.seq(config, _EMIT, "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    def autonomous_emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Autonomous-mode emit: the hands-off TRUST-ROOT chain build → admit →
        deploy. ADMISSION is the SOLE release gate (T-M4-10; governance §4/§5).

        Runs AFTER the (gated-spine) supervisor completes. There is NO human /
        recorded sign-off gate — the only release authority is the deploy-time
        :class:`AdmissionController`:

        1. **Guard** on the required gate-state (terminal Spec+Build+Test AND the
           tier-scaled assurance family) all PASS, newest-by-Seq on the
           trust-filtered ``gates`` channel (runner-attested only, L5). A halted /
           failed run (a stable-FAIL latch, the convergence bound spent, a missing
           verdict) fails this guard → emit NOTHING (no build, no admit, no deploy).
        2. **Build** the integrated subject hands-off via :class:`TrustedBuilder`
           over the pinned inputs keyed to the integrated ``tree_sha`` the gates
           ran over → a signed, provenance-bound :class:`BuildOutput`.
        3. **Admit** via :class:`AdmissionController` — re-verifying the build
           signature + every required gate's attestation PASS-for-THIS-tree + the
           cross-fork disqualification latch for the EXACT subject digest.
           Admission is the SOLE release gate: on DENY emit NOTHING (fail-closed;
           no deploy).
        4. **Deploy** ONLY an admitted decision via :class:`Deployer` — emit the
           deploy ``ExternalAction`` (idempotent; the outbox is the only
           outside-world surface — no real deploy here).

        The deploy idempotency key is the deployer's deterministic
        ``content_digest`` over (subject_digest, target) — process-stable (L7), so
        a kill-then-resume re-derives the SAME key and ``union_actions`` coalesces
        it: NO duplicate deploy. A halted / DENY'd run produces NO deploy action.
        """
        assert builder is not None  # narrowed by the autonomous-mode ctor check
        assert admission is not None
        assert deployer is not None
        assert gate_verifier is not None

        # -- 1. Guard: every required gate PASS (fail-closed) ------------------
        # A halted / failed run (latch, bound spent, missing verdict) fails here.
        if not _gates_all_pass(_latest_by_name(state), _required_gate_names(state)):
            return {"phase": "halted"}

        # A human REJECT at the release checkpoint (plan [10]) HALTS the hands-off
        # deploy even though admission would pass — subtractive only (it can never
        # turn a gate FAIL into a deploy; the gate guard above is unchanged).
        if checkpoint_blocks_release(state):
            return {"phase": "halted"}

        # -- 1b. Domain-pack enforcement (T-M5-08 / M5-R1-01): the pack release ---
        # requirements (approver-of-record, perceptual HITL) are enforced
        # UNCONDITIONALLY on the autonomous path — exactly like the gated/assistant
        # emit paths above call ``pack_release_admitted`` unconditionally. The
        # autonomous path is the ONLY one with no human in the loop, so it must NEVER
        # hands-off deploy a regulated/class-4/class-5 request whose human requirement
        # is unmet. We halt if EITHER (a) the pack does not admit the release
        # (``not pack_release_admitted`` — a missing recorded human approver-of-record
        # / perceptual HITL) OR (b) hands-off Autonomous is unreachable under the
        # composed veto (``not autonomous_pack_reachable`` — a regulated request).
        # This is an OR, not the prior AND-coupling: a FUTURE pack that binds a
        # class-4 approver WITHOUT vetoing Autonomous (so ``autonomous_pack_reachable``
        # is True) still cannot short-circuit the approver check — the
        # ``pack_release_admitted`` clause halts it. Build NOTHING, admit NOTHING,
        # deploy NOTHING.
        veto_seq = rt.seq(config, _EMIT, "pack:autonomous_reachable")
        if not pack_release_admitted(
            state, config, pack_registry, requested_mode=mode, ai_judge=pack_ai_judge
        ) or not autonomous_pack_reachable(
            state, pack_registry, requested_mode=mode, seq=veto_seq
        ):
            return {"phase": "halted"}

        subject = _pr_subject(state)
        if subject is None:
            return {"phase": "halted"}

        all_gates = _autonomous_attested_gates(state)
        bound = _admission_tree_and_gates(all_gates)
        if bound is None:
            # Impossibly (post-guard) no attested verdict carries the integrated
            # tree — fail-closed, deploy nothing.
            return {"phase": "halted"}
        tree_sha, gate_results, expected_gate_trees = bound
        # The required gates admission re-verifies are the FULL attested gate-state
        # across BOTH trees (terminal + assurance), sorted for a deterministic,
        # resume-stable tuple. ``expected_gate_trees`` pins each to ITS run_against
        # so admission's input-binding re-verifies the TERMINAL Spec/Build/Test
        # gates against the terminal tree too (M4-R1-01), not just the assurance
        # group bound to the provenance tree.
        required_gates = tuple(sorted({g.name for g in gate_results}))

        # -- Mandatory floor (M4-R1-02): admission ALONE enforces a floor ------
        # The terminal gates (Spec+Build+Test) AND the assurance integrity floor
        # (Security+Audit) MUST be in the admitted required set — fail-closed if
        # any is absent, so admission never rests on a data-derived set that
        # happened to omit a mandatory gate. (The pre-guard `_gates_all_pass`
        # already required the tier set PASS; this asserts the floor is part of
        # what admission cryptographically re-verifies, regardless of landed data.)
        if not MANDATORY_ADMISSION_FLOOR.issubset(required_gates):
            return {"phase": "halted"}

        # -- 2. Build the integrated subject hands-off (signed provenance) -----
        build_seq = rt.seq(config, _EMIT, "build:autonomous")
        request = _autonomous_build_request(state, tree_sha, build_seq)
        build_out = builder.build(
            request,
            # NB (M4-R1-03): these are the attested verdicts' ``results_digest``
            # content identities, NOT the WORM-anchored gate-result handle URIs
            # (gov §4). The real WORM store wiring (the handle/identity URIs) lands
            # with the WORM store (§0.3); the build provenance binding is intact
            # either way (the digests are content-addressed inputs).
            gate_result_uris=tuple(g.results_digest for g in gate_results if g.results_digest),
            scanner_config_rev=_scanner_config_rev(gate_results),
            seq=build_seq,
        )

        # -- 3. Admit — the SOLE release gate (fail-closed on DENY) ------------
        # Admission re-verifies EVERY required gate (signature + PASS + bound to
        # its expected tree) across BOTH the terminal and assurance trees, so a
        # forged / missing / wrong-tree TERMINAL gate now ALSO fails admission,
        # not just the upstream emit guard.
        admit_seq = rt.seq(config, _EMIT, "admission:autonomous")
        decision = admission.admit(
            build_out.result.subject_digest,
            provenance=build_out.provenance,
            build_verdict=build_out.signed_verdict,
            gate_results=gate_results,
            required_gates=required_gates,
            records=runner.records,
            verifier=gate_verifier,
            expected_gate_trees=expected_gate_trees,
            scanner_config_rev=_scanner_config_rev(gate_results),
            seq=admit_seq,
        )
        if not decision.admitted():
            # ADMISSION DENIED — emit NOTHING (no deploy). The trust root is the
            # only release authority; a denied artifact ships nothing (gov §5).
            # Cross-fork defense (gov §5): if the deny indicates a FORGERY / tamper
            # / wrong-tree replay (not an honest gate failure), disqualify the digest
            # in the cross-fork ledger so a sibling fork re-presenting it under a
            # fresh run_id is also denied — the trusted WRITE that makes
            # admission_look's disqualification half live (previously inert).
            if decision.reason in FORGERY_DENY_REASONS:
                runner.records.disqualify_digest(
                    build_out.result.subject_digest, decision.reason, admit_seq.run_id
                )
            return {"phase": "halted"}

        # -- 4. Deploy ONLY the admitted decision (idempotent) ----------------
        # A content-addressed handle for the admitting decision (DS9), a pure
        # function of its content identity so resume re-derives the same handle.
        decision_uri = f"cas://admission/{content_digest(decision.model_dump(mode='json'))}"
        _result, action = deployer.deploy(
            decision,
            admission_decision_uri=decision_uri,
            seq=rt.seq(config, _EMIT, "action:deploy"),
        )
        return {"phase": "done", "actions": [action]}

    _emit_node = autonomous_emit if autonomous else (gated_emit if gated else emit)

    # The terminal documentation node (task [3]). Authors the company-shaped doc
    # set + manifest IN-GRAPH against the terminal state, so the Technical Writer's
    # model spend goes through the SAME budget-metered proxy seam and the authored
    # doc artifacts land on the (checkpointed, outbox-governed) ``artifacts``
    # channel — rather than out-of-graph after ``invoke`` (the audit finding).
    # BEST-EFFORT: the node swallows any assembly failure (a docs failure must
    # never fail the build / block the PR), so it can edge straight into emit.
    _docs_node = make_documentation_node(rt, sink=doc_sink)

    def docs(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Terminal documentation node — a thin concrete ``def`` (the SG2/IM5
        ``add_node`` mypy seam: a closure typed as the ``NodeFn`` alias trips
        ``add_node``'s overload resolution). Delegates to the factory-built docs
        node, which assembles the bundle IN-GRAPH (best-effort) and returns the
        authored doc artifacts on the ``artifacts`` channel."""
        return _docs_node(state, config)

    def _route_after_factory(state: FactoryState, config: RunnableConfig) -> str:
        """Run the docs node only on the PASS path (governance: doc authoring is a
        post-release deliverable). A halted / failed run skips authoring (no writer
        spend, no doc artifacts) and goes straight to emit, which itself fail-closes
        on the terminal-gate guard."""
        return _DOCS if terminal_gates_pass(state) else _EMIT

    g: StateGraph[FactoryState] = StateGraph(FactoryState)
    g.add_node(_FACTORY, supervisor)
    g.add_node(_DOCS, docs)
    g.add_node(_EMIT, _emit_node)
    g.add_edge(START, _FACTORY)
    # PASS -> docs -> emit; non-PASS -> emit (skip authoring). Both paths reach the
    # emit node, whose own guard decides whether a PR/release is emitted.
    g.add_conditional_edges(_FACTORY, _route_after_factory, {_DOCS: _DOCS, _EMIT: _EMIT})
    g.add_edge(_DOCS, _EMIT)
    g.add_edge(_EMIT, END)

    return g.compile(checkpointer=checkpointer)
