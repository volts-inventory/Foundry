"""Architecture-layer artifacts (Phase 0 subset: pinned constraints).

The full SystemDesign/ADR/InterfaceContract models land with the
architecture agents (Phase 1); `PinnedConstraint` is here now because the
state backbone's `_merge_constraints` reducer is typed against it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class ConstraintScope(StrEnum):
    GLOBAL = "global"  # always injected verbatim into every node (arch §8)
    LOCAL = "local"  # relevance-scoped under the per-node injection budget


# Deterministic cross-cutting pattern check: a constraint matching these that
# is NOT marked global is a validated-classification mismatch -> HITL trip
# (arch §8, AI-R10-F1). Patterns are deliberately broad; false positives cost
# one HITL look, false negatives cost a scoping miss on a security invariant.
_CROSS_CUTTING = re.compile(
    r"\b(money|monetary|cents|currency|PII|personal data|logging|logs?|auth(entication|orization)?|"
    r"crypto(graphy)?|secret|credential|token)\b",
    re.IGNORECASE,
)


class PinnedConstraint(FrozenModel):
    """Injected VERBATIM, never summarized; superseded, never edited."""

    id: str
    text: str
    scope: ConstraintScope
    source: str  # ADR / criterion / ownership-map ref
    supersedes: str | None = None
    seq: Seq

    def misclassified_local(self) -> bool:
        """True when text matches cross-cutting patterns but scope is local."""
        return self.scope is ConstraintScope.LOCAL and bool(_CROSS_CUTTING.search(self.text))


class SystemDesign(FrozenModel):
    """Principal Architect output (agents.md Layer 2): the chosen architecture
    and component decomposition. The full design body (diagrams, data model,
    cross-cutting concerns) is content-addressed (DS9); only the component
    seam names and the body handle live in state. Superseded, never edited."""

    title: str
    design_uri: str  # CAS handle to the full design body (DS9)
    components: tuple[str, ...] = ()  # component decomposition (named seams)
    adr_ids: tuple[str, ...] = ()  # decisions this design rests on
    interface_ids: tuple[str, ...] = ()  # interface contracts this design defines
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class ADR(FrozenModel):
    """Architecture Decision Record (agents.md Layer 2 — Architect SOP writes
    ADRs WITH rejected alternatives). The decision narrative body is
    content-addressed (DS9); `rejected_alternatives` and their rationale are
    pinned downstream verbatim (architecture.md §8), so they stay in state."""

    id: str
    title: str
    decision: str  # the decision in one line; full narrative lives at body_uri
    body_uri: str  # CAS handle to the full ADR body / rationale (DS9)
    rejected_alternatives: tuple[str, ...] = ()  # alternatives considered + rejected
    status: str = "accepted"  # proposed | accepted | superseded
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class InterfaceContract(FrozenModel):
    """A stable interface seam definition (agents.md Layer 2): the API/interface
    contract downstream implementation branches code against. The signature/spec
    body is content-addressed (DS9). Frozen + versioned: a contract change
    arrives only as a supersession, never an in-place edit."""

    name: str  # the seam's stable identity (e.g. "TodoStore.add")
    spec_uri: str  # CAS handle to the full signature / contract body (DS9)
    kind: str = ""  # function|class|type|http|cli|event
    signature: str = ""  # written in the chosen language; compiled into wave-0 stubs
    consumers: tuple[str, ...] = ()  # component/interface ids that depend on this seam
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class OwnershipMap(FrozenModel):
    """File/interface → owner mapping (agents.md Layer 2/3): the Architect
    assigns each path/seam an owner so the Senior Engineer can resolve
    integration conflicts and derive a DETERMINISTIC merge order for
    branch-per-task implementation. Frozen + versioned; superseded, never
    edited. The merge order is a pure function of the mapping (no process
    state), so a LangGraph resume re-derives the identical order."""

    owners: dict[str, str]  # path / interface name -> owner (task or agent id)
    # unit -> the acceptance-criterion ids it implements (the planner authors it,
    # so lead decomposition is deterministic — no model call).
    unit_criteria: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def owner_of(self, unit: str) -> str | None:
        """Owner of a file/interface unit, or None if unowned."""
        return self.owners.get(unit)

    def merge_order(self) -> tuple[str, ...]:
        """Ownership units in a deterministic merge order.

        Ordered by (owner, unit) so units sharing an owner merge together and
        the sequence is a stable, total function of the mapping alone — the
        Senior Engineer's integration order is reproducible across resume
        (agents.md Layer 3; no reliance on dict insertion or process state)."""
        return tuple(sorted(self.owners, key=lambda unit: (self.owners[unit], unit)))

    # -- org fan-out derivation (plan: org_fanout_design.md, Wave A) -----------
    # Areas/leads are DERIVED on demand (a pure function of ``owners``), never a
    # stored field — so this adds NO serialization change and churns NO digest
    # (the ownership_map record is byte-identical to before). Every result is a
    # deterministic function of the mapping alone, so a resume re-derives the
    # identical org tree. Nothing consumes this yet (ships dark).

    def leads(self) -> tuple[str, ...]:
        """Distinct owners — one Tech-Lead per domain — in deterministic order."""
        return tuple(sorted(set(self.owners.values())))

    def units_of(self, owner: str) -> tuple[str, ...]:
        """Ownership units assigned to ``owner``, in merge order."""
        return tuple(u for u in self.merge_order() if self.owners[u] == owner)

    def area_of(self, owner: str) -> str:
        """Canonical area an owner's domain belongs to (``backend`` default)."""
        return classify_area(owner, self.units_of(owner))

    def areas_present(self) -> tuple[str, ...]:
        """Distinct areas that actually have ≥1 domain, in canonical order.

        Areas materialize from the work: an area appears here only when some
        owner classifies into it (so a headless service has no ``frontend``,
        a static site no ``data`` — no idle departments)."""
        present = {self.area_of(owner) for owner in self.leads()}
        return tuple(area for area in CANONICAL_AREAS if area in present)

    def leads_by_area(self) -> dict[str, tuple[str, ...]]:
        """area -> the owners (Tech-Leads) in it, in canonical area order.

        The org tree the fan-out reads: one entry per *present* area, each
        listing its domains' leads. Tiny areas (one lead) still appear — the
        manager-vs-report-to-PM decision is a later wave's, not this mapping's."""
        grouped: dict[str, list[str]] = {}
        for owner in self.leads():
            grouped.setdefault(self.area_of(owner), []).append(owner)
        return {area: tuple(grouped[area]) for area in CANONICAL_AREAS if area in grouped}


# --------------------------------------------------------------------------- #
# Area classification (org_fanout_design.md, Wave A)                           #
# --------------------------------------------------------------------------- #
# A domain (OwnershipMap owner) is classified into a canonical AREA from the
# names of the units it owns, reusing the SAME capability vocabulary that routes
# the domain packs (frontend / ml_data / regulated) so the org and the governance
# packs speak one language. Pure + deterministic: a fixed precedence resolves a
# multi-match, ``backend`` is the fallback, and the function depends only on its
# inputs (no wall-clock, no process state) so a resume re-derives identically.

#: Canonical areas in display / tree order (``backend`` is the default bucket).
CANONICAL_AREAS: tuple[str, ...] = ("frontend", "backend", "data", "infra", "security")
_DEFAULT_AREA = "backend"

#: The three engineering DISCIPLINE LANES the implementation subgraph fans out over
#: (one lead node + one engineer node each). Every canonical area folds into exactly
#: one lane, so every owner/task is covered by exactly one lane node: ``infra`` and
#: ``security`` join ``backend`` (matching the office's discipline rooms). Fixed order.
ENGINEERING_LANES: tuple[str, ...] = ("frontend", "backend", "data")
_AREA_TO_LANE: dict[str, str] = {
    "frontend": "frontend", "backend": "backend", "data": "data",
    "infra": "backend", "security": "backend",
}


def lane_of_area(area: str) -> str:
    """Discipline LANE (frontend/backend/data) an area folds into — a total map, so
    every owner lands in exactly one lane node (unknown/infra/security -> backend)."""
    return _AREA_TO_LANE.get(area, "backend")

#: (area, keywords) in PRECEDENCE order — first match wins (most-specific first).
#: Matching is WHOLE-TOKEN ONLY (a path is split into tokens on /_-. and each
#: keyword must equal a token). No substring matching: it false-fires on real
#: paths (``token`` ⊂ ``tokenizer``, ``view`` ⊂ ``interview``, ``data`` ⊂
#: ``metadata``), and shipping wrong labels into a content-addressed system is
#: worse than a ``backend`` fallback. Keywords therefore include common variants
#: (``view``/``views``) and exclude genuinely ambiguous tokens (``client`` —
#: api_client; ``store`` — storefront; ``model`` — domain model; ``feature`` —
#: feature flag; ``render``). A miss costs one backend label; a false hit
#: corrupts the org tree, so the set errs toward precision over recall.
_AREA_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "security",
        frozenset({
            "auth", "authn", "authz", "oauth", "crypto", "cryptography",
            "secret", "secrets", "credential", "credentials", "token", "tokens",
            "security", "compliance", "regulated", "fintech", "finance",
            "financial", "payment", "payments", "billing", "phi", "pii", "kms",
            "vault", "login", "signin", "signup", "session", "sessions", "rbac",
            "permission", "permissions",
        }),
    ),
    (
        "frontend",
        frozenset({
            "ui", "ux", "web", "mobile", "frontend", "view", "views",
            "component", "components", "page", "pages", "css", "html", "widget",
            "widgets", "screen", "screens", "button", "buttons", "layout",
            "layouts", "nav", "navbar", "theme", "themes", "stylesheet",
            "stylesheets",
        }),
    ),
    (
        "data",
        frozenset({
            "data", "dataset", "datasets", "etl", "warehouse", "schema",
            "schemas", "migration", "migrations", "sql", "analytics", "ingest",
            "ingestion", "ml", "training", "ai", "query", "queries", "database",
            "databases", "embedding", "embeddings",
        }),
    ),
    (
        "infra",
        frozenset({
            "infra", "infrastructure", "deploy", "deployment", "deployments",
            "ops", "devops", "sre", "platform", "docker", "dockerfile", "k8s",
            "kubernetes", "terraform", "helm", "cluster", "gateway", "proxy",
            "network", "networking", "provision", "provisioning", "ci", "cd",
            "pipeline", "pipelines",
        }),
    ),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(*texts: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens across all inputs (paths split on / _ - .)."""
    out: set[str] = set()
    for text in texts:
        out.update(_TOKEN_RE.findall(text.lower()))
    return frozenset(out)


def classify_area(owner: str, units: Iterable[str]) -> str:
    """Canonical area for a domain, from its owner id + the units it owns.

    Both the owner identifier AND every unit path contribute tokens, so an owner
    literally named ``auth_team`` classifies as ``security`` even before its
    files are considered. Deterministic: scans the area rules in fixed precedence
    (security → frontend → data → infra), returns the first whole-token match,
    else ``backend``."""
    tokens = _tokens(owner, *units)
    for area, keywords in _AREA_RULES:
        if tokens & keywords:  # whole-token intersection — no substring matching
            return area
    return _DEFAULT_AREA


def classify_areas(owners: Mapping[str, str]) -> dict[str, str]:
    """owner -> area for every distinct owner in an ``OwnershipMap.owners`` map.

    A standalone, pure mirror of :meth:`OwnershipMap.area_of` over the whole
    mapping (handy for telemetry / tests); returns owners in sorted order."""
    units_by_owner: dict[str, list[str]] = {}
    for unit, owner in owners.items():
        units_by_owner.setdefault(owner, []).append(unit)
    return {
        owner: classify_area(owner, units_by_owner[owner])
        for owner in sorted(units_by_owner)
    }
