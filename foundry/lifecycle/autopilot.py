"""Autopilot — Director proposes, a design panel votes, the factory builds.

The missing idea source for FULL automation (the milestone loop grows a build
toward a fixed target; nothing invents the NEXT target). Autopilot closes the
loop with a two-stage advisory ideation step between releases:

1. **Director** — one schema-constrained call proposing K CANDIDATE next
   actions (title + rationale + concrete scope) from the target + the release
   evidence so far. Candidates, not a single suggestion: the panel needs
   alternatives to discriminate between, not a yes/no to rubber-stamp.
2. **Panel** — N persona votes (staff software engineer, usability designer,
   product/game designer, QA lead by default; configurable). Each panelist
   scores every candidate 0-10, may VETO with a reason, and may offer one
   amendment. One schema call per panelist.
3. **Tally** — DETERMINISTIC, no model: a candidate vetoed by a strict
   majority is dead; the winner is the highest mean score above ``min_score``;
   ties break by director order (stable). If NOTHING clears the floor the
   autopilot STOPS ("panel declined") instead of building something the panel
   hated — fail-closed escalation to the human, mirroring the gate posture.

Trust posture: everything here is ADVISORY ideation (purpose="ideation" proxy
calls; no GateResult, no channel writes). The winning candidate only becomes
the next iteration's REQUEST + steering notes; the deterministic gates still
own every release, and the loop stops at the first gate refusal. Panel votes
are recorded in the report so a human can audit WHY each iteration was chosen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "AutopilotReport",
    "Candidate",
    "DEFAULT_PANEL",
    "IterationRecord",
    "PanelDecision",
    "PanelVote",
    "autopilot",
    "propose_candidates",
    "run_panel",
    "tally",
]

# (name, persona) — the default review board. Software + usability by design.
DEFAULT_PANEL: tuple[tuple[str, str], ...] = (
    (
        "staff_engineer",
        "a staff software engineer judging architecture fit, regression risk, and "
        "whether the codebase can absorb the change cleanly",
    ),
    (
        "usability_designer",
        "a usability/UX designer judging player/user experience: onboarding, "
        "friction, feedback loops, and clarity",
    ),
    (
        "product_designer",
        "a product/game designer judging whether the change makes the product more "
        "fun, coherent, and worth returning to",
    ),
    (
        "qa_lead",
        "a QA lead judging stability, testability, and whether the change is "
        "verifiable by the gates",
    ),
)

_DIRECTOR_SOP = (
    "You are the product DIRECTOR for an autonomous software factory. Given the "
    "target and the release history, propose ALTERNATIVE candidate next actions — "
    "distinct, concrete, independently buildable increments. Reply with JSON only."
)

_DIRECTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "minItems": 2,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["title", "rationale", "scope"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

_PANELIST_SOP = (
    "You are {persona}, on the review panel of an autonomous software factory. "
    "Score EVERY candidate 0-10 from your discipline's lens, veto only what you "
    "believe would actively harm the product, and optionally offer ONE amendment "
    "to your preferred candidate. Reply with JSON only."
)

_PANELIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 10}},
        "veto_index": {"type": "integer", "minimum": -1},
        "veto_reason": {"type": "string"},
        "amendment": {"type": "string"},
    },
    "required": ["scores"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    title: str
    rationale: str
    scope: str


@dataclass(frozen=True, slots=True)
class PanelVote:
    panelist: str
    scores: tuple[int, ...]  # one per candidate, director order
    veto_index: int = -1  # -1 == no veto
    veto_reason: str = ""
    amendment: str = ""


@dataclass(frozen=True, slots=True)
class PanelDecision:
    winner: Candidate | None  # None == panel declined everything (fail-closed)
    winner_index: int = -1
    mean_scores: tuple[float, ...] = ()
    amendments: tuple[str, ...] = ()  # amendments from panelists, winner-directed
    votes: tuple[PanelVote, ...] = ()

    @property
    def declined(self) -> bool:
        return self.winner is None


def _parsed_or_lenient(result: Any) -> dict[str, Any] | None:
    parsed = getattr(result, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(result, "text", "") or ""
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def propose_candidates(proxy: Any, target: str, evidence: str) -> list[Candidate]:
    """Director stage: K alternative next actions. Fail-closed on no plan."""
    result = proxy.complete(
        "autopilot_director",
        "T1",
        _DIRECTOR_SOP,
        [
            {
                "role": "user",
                "content": (
                    f"TARGET:\n{target}\n\nRELEASE HISTORY / EVIDENCE:\n{evidence}\n\n"
                    "Propose 3 distinct candidate next actions."
                ),
            }
        ],
        purpose="ideation",
        schema=_DIRECTOR_SCHEMA,
    )
    parsed = _parsed_or_lenient(result) or {}
    raw = parsed.get("candidates")
    out: list[Candidate] = []
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, dict) and str(c.get("title", "")).strip():
            out.append(
                Candidate(
                    title=str(c["title"]).strip(),
                    rationale=str(c.get("rationale", "")).strip(),
                    scope=str(c.get("scope", "")).strip(),
                )
            )
    if not out:
        raise ValueError("director produced no usable candidates (fail-closed)")
    return out


def run_panel(
    proxy: Any,
    target: str,
    evidence: str,
    candidates: Sequence[Candidate],
    *,
    panel: Sequence[tuple[str, str]] = DEFAULT_PANEL,
) -> list[PanelVote]:
    """One schema call per panelist; a malformed ballot is DROPPED (an absent
    juror, logged in the report by omission), never guessed."""
    listing = "\n".join(
        f"[{i}] {c.title}\n    rationale: {c.rationale}\n    scope: {c.scope}"
        for i, c in enumerate(candidates)
    )
    votes: list[PanelVote] = []
    for name, persona in panel:
        result = proxy.complete(
            f"autopilot_panel_{name}",
            "T2",
            _PANELIST_SOP.format(persona=persona),
            [
                {
                    "role": "user",
                    "content": (
                        f"TARGET:\n{target}\n\nEVIDENCE:\n{evidence}\n\n"
                        f"CANDIDATES:\n{listing}\n\nScore each candidate (array of "
                        f"{len(candidates)} integers, director order)."
                    ),
                }
            ],
            purpose="ideation",
            schema=_PANELIST_SCHEMA,
        )
        parsed = _parsed_or_lenient(result)
        if not parsed:
            continue
        raw_scores = parsed.get("scores")
        if not isinstance(raw_scores, list) or len(raw_scores) != len(candidates):
            continue
        try:
            scores = tuple(max(0, min(10, int(s))) for s in raw_scores)
        except (TypeError, ValueError):
            continue
        veto = parsed.get("veto_index", -1)
        veto = veto if isinstance(veto, int) and -1 <= veto < len(candidates) else -1
        votes.append(
            PanelVote(
                panelist=name,
                scores=scores,
                veto_index=veto,
                veto_reason=str(parsed.get("veto_reason", "")).strip(),
                amendment=str(parsed.get("amendment", "")).strip(),
            )
        )
    return votes


def tally(
    candidates: Sequence[Candidate],
    votes: Sequence[PanelVote],
    *,
    min_score: float = 5.0,
) -> PanelDecision:
    """DETERMINISTIC decision — no model in the loop.

    * a candidate vetoed by a STRICT MAJORITY of cast ballots is dead;
    * winner = highest mean score among the living, must clear ``min_score``;
    * tie → the earliest in director order (stable across reruns);
    * no ballots at all, or nothing clears the floor ⇒ DECLINED (fail-closed —
      autopilot stops and escalates to the human rather than guessing).
    """
    if not votes:
        return PanelDecision(winner=None, votes=())
    n = len(candidates)
    veto_counts = [0] * n
    for v in votes:
        if 0 <= v.veto_index < n:
            veto_counts[v.veto_index] += 1
    majority = len(votes) / 2
    means = tuple(
        sum(v.scores[i] for v in votes) / len(votes) for i in range(n)
    )
    best_i, best_mean = -1, -1.0
    for i in range(n):
        if veto_counts[i] > majority:
            continue  # dead: a discipline's strict majority said "harmful"
        if means[i] > best_mean:  # strict > keeps ties on the EARLIER candidate
            best_i, best_mean = i, means[i]
    if best_i < 0 or best_mean < min_score:
        return PanelDecision(winner=None, mean_scores=means, votes=tuple(votes))
    amendments = tuple(v.amendment for v in votes if v.amendment)
    return PanelDecision(
        winner=candidates[best_i],
        winner_index=best_i,
        mean_scores=means,
        amendments=amendments,
        votes=tuple(votes),
    )


@dataclass(frozen=True, slots=True)
class IterationRecord:
    index: int  # 1-based
    request: str
    passed: bool
    out_dir: str
    decision: PanelDecision | None = None  # None on iteration 1 (the target itself)


@dataclass(frozen=True, slots=True)
class AutopilotReport:
    target: str
    iterations: tuple[IterationRecord, ...] = field(default=())
    stopped: str = ""  # why the loop ended

    @property
    def released_dir(self) -> str | None:
        released = [r for r in self.iterations if r.passed]
        return released[-1].out_dir if released else None

    def summary(self) -> str:
        lines = [f"autopilot: {len(self.iterations)} iteration(s) — stopped: {self.stopped}"]
        for r in self.iterations:
            chosen = f"  [panel: {r.decision.winner.title}]" if r.decision and r.decision.winner else ""
            lines.append(f"  {r.index:>2}. {'PASS' if r.passed else 'HALT'}  {r.request[:80]}{chosen}")
        return "\n".join(lines)


def _request_for(target: str, decision: PanelDecision) -> str:
    assert decision.winner is not None
    parts = [f"{target} — NEXT ITERATION: {decision.winner.title}. {decision.winner.scope}"]
    if decision.amendments:
        parts.append("PANEL AMENDMENTS (honor these): " + " | ".join(decision.amendments))
    return " ".join(parts)


def autopilot(
    target: str,
    *,
    out_dir: str,
    iterations: int = 3,
    proxy: Any | None = None,
    run_fn: Callable[..., Any] | None = None,
    panel: Sequence[tuple[str, str]] = DEFAULT_PANEL,
    min_score: float = 5.0,
    thread_id: str = "autopilot",
    **run_kwargs: Any,
) -> AutopilotReport:
    """The full loop: build the target, then Director → panel → build, repeated.

    Bounded by ``iterations``; chains each RELEASED tree as the next build's
    base (same seam as the milestone loop); stops fail-closed on the first gate
    refusal OR a panel decline — both are recorded as the ``stopped`` reason so
    the human knows exactly why the loop wants them back.
    """
    from pathlib import Path

    if run_fn is None:
        from foundry.cli import run as run_fn  # type: ignore[assignment]
    if proxy is None:
        raise ValueError("autopilot needs a proxy (the Director/panel ideation egress)")

    records: list[IterationRecord] = []
    prior_out: str | None = None
    request = target
    decision: PanelDecision | None = None
    stopped = f"iteration budget ({iterations}) reached"
    for i in range(1, max(1, iterations) + 1):
        it_out = str(Path(out_dir) / f"i{i:02d}")
        result = run_fn(
            request,
            output_dir=it_out,
            base_dir=prior_out,
            thread_id=f"{thread_id}-i{i:02d}",
            **run_kwargs,
        )
        passed = bool(getattr(result, "passed", False))
        records.append(
            IterationRecord(index=i, request=request, passed=passed, out_dir=it_out,
                            decision=decision)
        )
        if not passed:
            stopped = f"gates refused iteration {i} (fail-closed)"
            break
        prior_out = it_out
        if i == max(1, iterations):
            break
        evidence = "\n".join(
            f"iteration {r.index}: RELEASED — {r.request}" for r in records if r.passed
        )
        try:
            candidates = propose_candidates(proxy, target, evidence)
            votes = run_panel(proxy, target, evidence, candidates, panel=panel)
            decision = tally(candidates, votes, min_score=min_score)
        except Exception as exc:  # ideation is ADVISORY: stop gracefully, never crash
            stopped = f"ideation failed after iteration {i} ({exc}) — released work stands"
            break
        if decision.declined:
            stopped = "panel declined every candidate — human steering needed"
            break
        request = _request_for(target, decision)
    return AutopilotReport(target=target, iterations=tuple(records), stopped=stopped)
