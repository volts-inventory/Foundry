"""Agent-specific VALIDITY evals (evaluation.md §4) — distinct from the outcome.

The outcome eval (:mod:`foundry.evaluation.harness` / :mod:`ab_runner`) asks one
question: did the *pipeline* resolve the task? That is necessary but NOT
sufficient. A pipeline can resolve **by luck** while a role agent's contribution
was invalid (it approved a defective diff, it missed an omitted security
criterion, it authored a test that doesn't bind the criterion) — and, the mirror
image, an agent can do its job perfectly on a task the pipeline never resolves.
Outcome validity and *contribution* validity are therefore **orthogonal axes**
(``validity ⊥ outcome``), and evaluation.md §4 calls out the per-role checks the
generalist outcome eval misses.

This module measures, per ROLE agent, whether its CONTRIBUTION is valid against a
fixture carrying a **known planted defect/omission**:

* **Criteria-Completeness Critic** — a fixture whose PRD has a KNOWN
  security/abuse omission. A *valid* critic flags it under
  ``security_abuse_omissions`` (``CriteriaCritique.spec_gate_blocking()`` is
  True); an invalid critic returns a clean critique and MISSES it.
* **Reviewer / Critic** — a fixture with a planted defect in the diff. A *valid*
  reviewer returns ``request-changes`` (a ``GateStatus.FAIL`` verdict); an
  invalid reviewer approves and MISSES it.
* **QA Engineer** — a fixture with a HIGH-RISK acceptance criterion. A *valid* QA
  authors a criterion-traceable NEGATIVE oracle bound to that criterion (a
  negative oracle, not mere coverage); an invalid QA produces no negative oracle
  for it and MISSES the fidelity check.

Each per-role check is a **planted-defect-caught** assertion: we drive the REAL
role agent (a real :class:`ModelProviderProxy` over the existing test doubles —
LEARNINGS D1/D4, no live API key) twice — once scripted to CATCH and once
scripted to MISS — and the eval is **non-vacuous** iff it scores the catcher
valid and the misser invalid. A role whose responsible agent catches the planted
defect scores ``valid=True``; one that misses scores ``valid=False``.

:func:`agent_validity_report` aggregates the per-role validity into an
:class:`AgentValidityReport` (the ``factory_eval`` aggregator the scorecard /
Phase-1.5 gate consume), deterministic over the structured fixtures.

HONESTY NOTE (read before reuse): like the rest of the Phase-1.5 harness this is
a **methodology dry-run on synthetic fixtures** — the planted defects are
hand-authored, NOT drawn from a real labeled defect corpus. Every model defaults
``provenance == Provenance.DRY_RUN``; a report asserts NOTHING about real
agent quality. It only proves the per-role validity *check* is real and
non-vacuous (it distinguishes a catcher from a misser) and that contribution
validity is measured **independently of the pipeline outcome**.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from foundry.agents import AgentRuntime
from foundry.agents.planner import make_planner_node
from foundry.agents.quality import make_qa_engineer_node, make_reviewer_critic_node
from foundry.agents.quality.qa_engineer import ACCEPTANCE_CRITERION_KIND
from foundry.durability.artifact_store import ArtifactStore
from foundry.evaluation.hypothesis import Provenance
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, verdict_json
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateStatus
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import OracleKind
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

__all__ = [
    "AgentValidityReport",
    "RoleValidity",
    "ValidityRole",
    "agent_validity_report",
    "criteria_critic_validity",
    "qa_engineer_validity",
    "reviewer_critic_validity",
]

# Dev Zone-B key for the keyless judgment path (LEARNINGS D4). A throwaway dev
# key — the validity eval signs/verifies entirely in-process.
_KEY = b"dev-zone-b-agent-validity-key"
_KEY_ID = "dev-key-agent-validity"


class ValidityRole(StrEnum):
    """The role agents whose CONTRIBUTION validity §4 measures.

    These are the three roles with a directly-checkable planted-defect contract:
    the critic flags an omission, the reviewer catches a defect, the QA authors a
    criterion-traceable negative oracle. (Other roles — architect, senior — are
    measured by handoff/retrieval evals elsewhere in §4.)
    """

    CRITERIA_CRITIC = "criteria_completeness_critic"
    REVIEWER_CRITIC = "reviewer_critic"
    QA_ENGINEER = "qa_engineer"


class RoleValidity(FrozenModel):
    """One role agent's validity verdict against its planted-defect fixture.

    ``valid`` is True iff the responsible agent's actual contribution CAUGHT the
    planted defect (the critic flagged the omission / the reviewer returned
    request-changes / the QA bound a negative oracle to the high-risk criterion).
    ``detail`` records what was observed (audit provenance). This is DISTINCT from
    any pipeline outcome — a valid contribution is neither necessary nor
    sufficient for the pipeline to resolve (``validity ⊥ outcome``).
    """

    role: ValidityRole
    valid: bool
    planted_defect: str  # what the fixture planted (the thing a valid agent must catch)
    observed: str  # what the agent actually contributed (audit)
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1


class AgentValidityReport(FrozenModel):
    """Per-role agent-validity aggregate (evaluation.md §4 ``factory_eval`` slice).

    Aggregates the per-role :class:`RoleValidity` records into one frozen,
    content-addressable report. ``validity_score`` is the fraction of roles whose
    contribution was valid; ``all_valid`` is the conjunction. The report is
    DISTINCT from the outcome eval — it measures whether each agent did its job
    validly, not whether the pipeline resolved.

    HONESTY: ``provenance`` defaults to DRY_RUN; the planted defects are synthetic
    (module docstring), so the score asserts NOTHING about real agent quality —
    only that the per-role checks are real and non-vacuous.
    """

    roles: tuple[RoleValidity, ...]
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def validity_score(self) -> float:
        """Fraction of evaluated roles whose contribution was valid."""
        if not self.roles:
            return 0.0
        return sum(1 for r in self.roles if r.valid) / len(self.roles)

    @property
    def all_valid(self) -> bool:
        """True iff EVERY evaluated role's contribution was valid."""
        return bool(self.roles) and all(r.valid for r in self.roles)

    def role_validity(self, role: ValidityRole) -> RoleValidity:
        """The :class:`RoleValidity` for ``role`` (raises if not evaluated)."""
        for r in self.roles:
            if r.role is role:
                return r
        raise KeyError(f"role not evaluated: {role}")


# --------------------------------------------------------------------------- #
# shared harness                                                              #
# --------------------------------------------------------------------------- #


def _runtime(transport: MockTransport, *, cas: ArtifactStore | None = None) -> AgentRuntime:
    """A real :class:`AgentRuntime` over the doubles (no live API key, D1/D4).

    ``cas`` (an in-process :class:`ArtifactStore`) is wired for the QA check so the
    emitted ``OracleBinding`` bodies are persisted + resolvable for the
    criterion-traceability assertion; the critic/reviewer checks read their
    contribution off channel writes directly and need no store.
    """
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(_KEY, _KEY_ID))
    return AgentRuntime(proxy=proxy, cas=cas)


def _config(
    run_id: str,
    *,
    nonce: str | None = None,
    subject: str | None = None,
    state_sha: str | None = None,
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": run_id}
    if nonce is not None:
        configurable["verdict_nonce"] = nonce
    if subject is not None:
        configurable["subject_artifact_uri"] = subject
    if state_sha is not None:
        configurable["state_sha"] = state_sha
    return {"configurable": configurable, "metadata": {"langgraph_step": 1}}


def _seq(node_id: str) -> Seq:
    return Seq(run_id="validity", emission_epoch=0, superstep_index=0, node_id=node_id, local=0)


# --------------------------------------------------------------------------- #
# per-role validity checks (planted-defect-caught)                            #
# --------------------------------------------------------------------------- #

# The PRD-level security/abuse omission a VALID critic must flag.
_PLANTED_OMISSION = "no acceptance criterion covers rate-limiting / abuse of the add endpoint"


def criteria_critic_validity(*, catches: bool = True) -> RoleValidity:
    """Validity of the Criteria-Completeness Critic on a KNOWN security omission.

    The fixture's PRD has a planted security/abuse omission (:data:`_PLANTED_OMISSION`).
    The model reply is scripted to EITHER flag it (``catches=True``) or return a
    clean critique that MISSES it (``catches=False``) — so the eval is provably
    non-vacuous (it must score the catcher valid and the misser invalid).

    A *valid* critic flags the omission under ``security_abuse_omissions`` so its
    advisory ``GateTriage`` marks the Spec gate BLOCKING
    (``CriteriaCritique.spec_gate_blocking()`` is True — LEARNINGS PR3). The
    critic NEVER mints a ``GateResult`` (L5); validity is read off its OWN
    contribution (the critique handle + the blocking advisory), not a gate
    verdict.
    """
    omissions = [_PLANTED_OMISSION] if catches else []
    payload = {
        "intent": {"intent": "add todos"},
        "prd": {"title": "Todo API", "body": "Add a todo via a public REST endpoint."},
        "acceptance_criteria": [
            {"id": "AC-1", "text": "Given a todo, When POSTed, Then 201.", "oracle_hint": "201"}
        ],
        "criteria_review": {"missing": [], "security_abuse_omissions": omissions},
        "sprint_plan": {"milestones": [], "usd_cap_microusd": 0, "done_definition": "done"},
        "design": {"title": "Todo API", "body": "api"},
        "adrs": [],
        "interfaces": [],
        "ownership": [],
    }
    transport = MockTransport.scripted({}, default_text=_json(payload))
    rt = _runtime(transport)
    # The critic is the planner's criteria_review: one call authors the plan and
    # reviews its own criteria.
    node = make_planner_node(rt)

    state: FactoryState = {"request": "Add a todo via a public REST endpoint."}
    writes = node(state, _config("validity-critic"))

    # The critic's OWN contribution: did its advisory flag the omission as
    # Spec-gate BLOCKING? (Read off the GateTriage it emitted, not a verdict.)
    triages = writes.get("gate_triage") or []
    blocking = any(
        t.name == "spec" and t.explanation.startswith("BLOCKING") for t in triages
    )
    flagged = [t.explanation for t in triages]
    return RoleValidity(
        role=ValidityRole.CRITERIA_CRITIC,
        valid=blocking,
        planted_defect=f"security/abuse omission: {_PLANTED_OMISSION}",
        observed=(
            f"critic advisory blocking={blocking}; explanations={flagged}"
        ),
    )


# The planted defect a VALID reviewer must catch in the diff under review.
_PLANTED_DEFECT = "weakened assertion: test no longer binds the 401 criterion"


def reviewer_critic_validity(*, catches: bool = True) -> RoleValidity:
    """Validity of the Reviewer/Critic on a fixture with a PLANTED diff defect.

    The fixture's diff carries a known defect (:data:`_PLANTED_DEFECT`). The
    reviewer's verdict is scripted to EITHER request-changes (``catches=True``, a
    ``GateStatus.FAIL`` verdict) or approve (``catches=False``, MISSES it) — so
    the eval is non-vacuous.

    A *valid* reviewer returns ``request-changes`` (FAIL) with a finding. Validity
    is read off the reviewer's OWN proxy-stamped ``GateResult`` status — the agent
    never signs it (L5/Q2), the proxy stamps the binding; here we only inspect the
    verdict the reviewer's judgment produced, NOT whether the pipeline merged it.
    """
    if catches:
        verdict = verdict_json("request-changes", [_PLANTED_DEFECT])
    else:
        verdict = verdict_json("approve", [])
    transport = MockTransport(verdicts=[verdict])
    rt = _runtime(transport)
    node = make_reviewer_critic_node(rt)

    cfg = _config(
        "validity-reviewer",
        nonce="N-validity",
        subject="cas://diff/planted-defect",
        state_sha="sha-validity",
    )
    [result] = node({}, cfg)["gates"]
    caught = result.status is GateStatus.FAIL
    return RoleValidity(
        role=ValidityRole.REVIEWER_CRITIC,
        valid=caught,
        planted_defect=f"diff defect: {_PLANTED_DEFECT}",
        observed=f"reviewer verdict={result.status.value}; findings={list(result.findings)}",
    )


# The high-risk criterion the fixture plants; a VALID QA binds a NEGATIVE oracle.
_HIGH_RISK_CID = "AC-SEC-401"


def _high_risk_criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=_HIGH_RISK_CID,
        text="Given an unauthenticated request, When it hits /todos, Then 401.",
        criticality=Criticality.HIGH_RISK,
        oracle_hint="request without token returns 401; negative oracle",
        security_abuse=True,
        seq=_seq("product_manager"),
    )


def qa_engineer_validity(*, catches: bool = True) -> RoleValidity:
    """Validity of the QA Engineer's criterion-traceable NEGATIVE oracle.

    The fixture plants ONE high-risk criterion (:data:`_HIGH_RISK_CID`). The model
    reply is scripted to EITHER trace a test to it (``catches=True``) or trace no
    test to it (``catches=False``) — but the QA node's contract is that it binds a
    NEGATIVE oracle per HIGH-RISK criterion regardless of the model's traceability
    (LEARNINGS Q4: the criteria set is the source of truth, the node fails closed
    to coverage). So the catch/miss is driven by whether the HIGH-RISK criterion
    is PRESENT in the fixture: a valid QA, given a high-risk criterion, authors a
    negative oracle bound to it; given none (``catches=False`` swaps it to a NORMAL
    criterion), it authors none — the fidelity check the eval distinguishes.

    Validity is read off the QA's OWN artifacts: a ``criterion_id``-traceable
    NEGATIVE ``OracleBinding`` for the high-risk criterion. The QA NEVER writes a
    ``GateResult`` (L5/Q4); validity is its authored contribution, not a verdict.
    """
    criterion = (
        _high_risk_criterion()
        if catches
        # The miss case: the responsible agent is given a fixture WITHOUT the
        # high-risk criterion (a NORMAL one), so a correct QA authors no negative
        # oracle — the planted high-risk fidelity requirement is unmet.
        else AcceptanceCriterion(
            id=_HIGH_RISK_CID,
            text="Given a user, When they add a todo, Then it appears.",
            criticality=Criticality.NORMAL,
            seq=_seq("product_manager"),
        )
    )
    cas = ArtifactStore()
    handle = _criterion_handle(criterion, store=cas)
    state: FactoryState = {"artifacts": [handle]}

    # Trace a test to the criterion (the model's enrichment); the negative-oracle
    # binding is the node's own invariant on high-risk criteria, not the model's.
    payload = {"tests": [{"test_id": "t-401", "criterion_ids": [_HIGH_RISK_CID]}]}
    transport = MockTransport.scripted({}, default_text=_json(payload))
    rt = _runtime(transport, cas=cas)
    node = make_qa_engineer_node(rt, resolve=cas.resolve, run_against="task/validity")

    writes = node(state, _config("validity-qa"))
    artifacts = writes.get("artifacts") or []
    # The QA's contribution: a NEGATIVE oracle traceable to the high-risk criterion.
    # Resolve each oracle_binding body and check it BINDS _HIGH_RISK_CID as NEGATIVE
    # (criterion fidelity, not mere coverage — LEARNINGS Q4).
    negative_for_cid = _negative_oracle_present(artifacts, cas, _HIGH_RISK_CID)
    oracle_kinds = sorted(a.kind for a in artifacts)
    return RoleValidity(
        role=ValidityRole.QA_ENGINEER,
        valid=negative_for_cid,
        planted_defect=(
            f"high-risk criterion {_HIGH_RISK_CID} requires a criterion-traceable "
            "negative oracle (fidelity, not coverage)"
        ),
        observed=(
            f"negative_oracle_for_{_HIGH_RISK_CID}={negative_for_cid}; "
            f"artifact_kinds={oracle_kinds}"
        ),
    )


# --------------------------------------------------------------------------- #
# aggregation                                                                 #
# --------------------------------------------------------------------------- #


def agent_validity_report(
    *,
    catches: Mapping[ValidityRole, bool] | None = None,
    provenance: Provenance = Provenance.DRY_RUN,
) -> AgentValidityReport:
    """Aggregate the per-role validity checks into an :class:`AgentValidityReport`.

    Runs each role's planted-defect-caught check and folds the results. By default
    every responsible agent CATCHES its planted defect (a fully-valid factory); a
    per-role ``catches`` override scripts a MISS for a role (e.g. to assert the
    report flags an invalid contribution even when the rest are valid — the
    ``validity ⊥ outcome`` separation). Deterministic over the structured
    fixtures: the same ``catches`` map yields an identical (content-addressable)
    report.

    HONESTY: synthetic dry-run — the planted defects are hand-authored, so the
    score asserts NOTHING about real agent quality.
    """
    catches = catches or {}
    roles = (
        criteria_critic_validity(catches=catches.get(ValidityRole.CRITERIA_CRITIC, True)),
        reviewer_critic_validity(catches=catches.get(ValidityRole.REVIEWER_CRITIC, True)),
        qa_engineer_validity(catches=catches.get(ValidityRole.QA_ENGINEER, True)),
    )
    return AgentValidityReport(roles=roles, provenance=provenance)


# --------------------------------------------------------------------------- #
# small helpers                                                               #
# --------------------------------------------------------------------------- #


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _criterion_handle(
    criterion: AcceptanceCriterion, *, store: ArtifactStore | None = None
) -> Artifact:
    from foundry.agents._common import emit_artifact

    return emit_artifact(
        criterion, ACCEPTANCE_CRITERION_KIND, "product_manager", criterion.seq, store=store
    )


def _negative_oracle_present(
    artifacts: list[Artifact], cas: ArtifactStore, cid: str
) -> bool:
    """True iff some emitted ``oracle_binding`` BINDS ``cid`` as a NEGATIVE oracle.

    The QA emits each :class:`OracleBinding` as a content-addressed handle (DS9 /
    L4); its ``criterion_id`` / ``oracle_kind`` ride on the TYPED BODY, not the
    handle metadata. We resolve each ``oracle_binding`` handle through the wired
    in-process store and check the body binds ``cid`` with
    ``OracleKind.NEGATIVE`` — the criterion-fidelity contract a valid QA must meet
    (LEARNINGS Q4: a negative oracle per high-risk criterion, not mere coverage).
    """
    for a in artifacts:
        if getattr(a, "kind", None) != "oracle_binding":
            continue
        body = cas.get_body(a.uri)
        if (
            getattr(body, "criterion_id", None) == cid
            and getattr(body, "oracle_kind", None) is OracleKind.NEGATIVE
        ):
            return True
    return False
