"""Milestone loop — long-horizon autonomous growth. (ROADMAP Phase 5)

Until now the factory's only "keep going" mechanisms were the deepening loop
(bounded, single release) and ``evolve`` (re-gates a SUPPLIED change, authors
nothing). This module adds the authoring loop over both:

* **plan** — decompose a big target ("a Stardew-style moon-colony game") into an
  ORDERED milestone list (engine loop → world → farming → NPCs → economy →
  save/load → polish), via one schema-constrained proxy call — or take an
  explicit human-authored list (gated posture / tests).
* **execute** — milestone 1 is a normal factory run; milestone k>1 runs the
  full pipeline WITH the prior milestone's released tree as ``base_dir`` (the
  same seeded-base seam the Workshop's iterate uses), so each milestone builds
  ON the previous release instead of greenfield. Every milestone passes the
  same gates as any run — the loop only ever chains RELEASED trees, and it
  STOPS at the first milestone whose gates refuse (fail-closed; the report says
  where and why it stopped).

Durability rides the normal ``state_dir``/``thread_id`` seams — each milestone
gets a derived thread id, so a killed multi-day build resumes at the milestone
it died in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["MilestoneReport", "MilestoneResult", "plan_milestones", "run_milestones"]

_PLAN_SOP = (
    "You are a principal program manager decomposing a large software target into "
    "ordered, independently-releasable milestones. Each milestone must be a "
    "shippable increment that builds ON the previous ones (milestone 1 is the "
    "walking skeleton: the core loop running end to end). Reply with JSON only."
)

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "milestones": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string"},
        }
    },
    "required": ["milestones"],
    "additionalProperties": False,
}


def plan_milestones(
    request: str,
    proxy: Any,
    *,
    max_milestones: int = 8,
) -> list[str]:
    """One schema-constrained proxy call → the ordered milestone list.

    Fail-closed: an unparseable/empty plan raises (never a silent single-shot
    fallback that would masquerade as a planned multi-milestone build).
    """
    result = proxy.complete(
        "program_manager",
        "T1",
        _PLAN_SOP,
        [
            {
                "role": "user",
                "content": (
                    f"TARGET:\n{request}\n\nDecompose into at most {max_milestones} "
                    "ordered milestones."
                ),
            }
        ],
        purpose="ideation",
        schema=_PLAN_SCHEMA,
    )
    parsed = result.parsed if isinstance(result.parsed, dict) else None
    if parsed is None:
        try:
            parsed = json.loads(result.text or "")
        except (json.JSONDecodeError, ValueError):
            parsed = None
    milestones = (parsed or {}).get("milestones")
    if not isinstance(milestones, list) or not milestones:
        raise ValueError(
            "milestone planning returned no usable plan (fail-closed; pass an "
            "explicit milestone list or retry)"
        )
    return [str(m).strip() for m in milestones if str(m).strip()][:max_milestones]


@dataclass(frozen=True, slots=True)
class MilestoneResult:
    index: int  # 1-based
    title: str
    passed: bool
    out_dir: str


@dataclass(frozen=True, slots=True)
class MilestoneReport:
    request: str
    results: tuple[MilestoneResult, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def released_dir(self) -> str | None:
        """The LAST released tree (the furthest gated increment), or None."""
        released = [r for r in self.results if r.passed]
        return released[-1].out_dir if released else None

    def summary(self) -> str:
        lines = [f"milestones: {len(self.results)} run"]
        for r in self.results:
            lines.append(f"  {r.index:>2}. {'PASS' if r.passed else 'HALT'}  {r.title}")
        lines.append(f"result: {'ALL RELEASED' if self.passed else 'STOPPED AT FIRST REFUSAL'}")
        return "\n".join(lines)


def run_milestones(
    request: str,
    *,
    out_dir: str,
    milestones: Sequence[str] | None = None,
    proxy: Any | None = None,
    run_fn: Callable[..., Any] | None = None,
    thread_id: str = "milestones",
    max_milestones: int = 8,
    **run_kwargs: Any,
) -> MilestoneReport:
    """Drive the factory milestone-by-milestone, chaining released trees.

    ``milestones`` explicit ⇒ used verbatim (gated posture / tests); otherwise
    planned via ``proxy`` (required then). ``run_fn`` defaults to
    :func:`foundry.cli.run`; injectable for tests. Extra ``run_kwargs``
    (backend, adapter, mode, state_dir, ...) pass through to every
    milestone's run. Stops at the first milestone whose gates refuse — the
    report's ``released_dir`` is the furthest RELEASED increment either way.
    """
    from pathlib import Path

    if run_fn is None:
        from foundry.cli import run as run_fn  # type: ignore[assignment]
    plan = list(milestones) if milestones else None
    if plan is None:
        if proxy is None:
            raise ValueError("run_milestones needs either an explicit plan or a proxy to plan with")
        plan = plan_milestones(request, proxy, max_milestones=max_milestones)
    plan = plan[:max_milestones]

    plan_text = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(plan))
    results: list[MilestoneResult] = []
    prior_out: str | None = None
    for i, title in enumerate(plan, start=1):
        m_out = str(Path(out_dir) / f"m{i:02d}")
        milestone_request = f"{request} — MILESTONE {i}/{len(plan)}: {title}"
        spec = (
            f"OVERALL TARGET:\n{request}\n\nMILESTONE PLAN:\n{plan_text}\n\n"
            f"THIS BUILD implements ONLY milestone {i}: {title}. Build on the "
            "existing base tree; do not regress released behavior (the prior "
            "criteria ride as the regression floor)."
        )
        result = run_fn(
            milestone_request,
            spec=spec,
            output_dir=m_out,
            base_dir=prior_out,
            thread_id=f"{thread_id}-m{i:02d}",
            **run_kwargs,
        )
        passed = bool(getattr(result, "passed", False))
        results.append(MilestoneResult(index=i, title=title, passed=passed, out_dir=m_out))
        if not passed:
            break  # fail-closed: never build milestone k+1 on an unreleased tree
        prior_out = m_out
    return MilestoneReport(request=request, results=tuple(results))
