"""Spec gate — class 1 (config-evaluable). (governance §1, plan §5.1)

Evaluates the acceptance-criteria document deterministically: every criterion
must carry an id, testable text, and an oracle hint; a criterion written as a
structured BDD scenario (any of ``given``/``when``/``then`` present) must have at
least one step of each; and **security/abuse
criteria coverage is mandatory where applicable — omission fails the gate**
(governance §1b: "100% criterion-coverage" cannot be satisfied by
under-specifying).

A SECOND input is honored: the Criteria-Completeness Critic's findings — its
``security_abuse_omissions`` (``CriteriaCritique.spec_gate_blocking()``). A
critic-flagged applicable security/abuse omission FAILS the Spec gate
(governance §1b / agents.md Layer 1 — Criteria-Completeness Critic SOP). The
critic emits findings ONLY (LEARNINGS L5); it never mints a verdict, so the
gate reads the critique BODY as a materialized input (a ``critique`` block on
the criteria document) and re-derives the FAIL itself.

Two-zone split: zone A (``execute``) only reads + parses the criteria
document (criteria list + critique block) and reports the raw parsed payload;
zone B (``decide``) applies the completeness rules — the verdict is a pure
function of the observation, never a bit the document (an agent-authored
artifact) self-reports. The critique block carries the critic's RAW findings
(``security_abuse_omissions``), not a verdict bit: zone B alone turns a
non-empty omission list into a FAIL, so an agent cannot forge a PASS by
omitting the block (its absence is just "no flagged omission", and the
structural security-criterion rule still independently guards coverage).

Dev-grade scope (plan §0.3): this is the *structural* half of the Spec gate.
The LLM half (independent criteria critique, Given/When/Then quality) is a
judgment seam defended separately (critic + self-judge stamp in the chosen
profile); it is never folded into this runner-signed verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe

__all__ = ["SpecGate"]

_REQUIRED_FIELDS = ("id", "text", "oracle")
_SCENARIO_STEPS = ("given", "when", "then")

# The critique block the materializer writes onto the criteria document from the
# Criteria-Completeness Critic's findings. Zone A surfaces the RAW omission list;
# zone B alone turns a non-empty list into a FAIL (the critic mints no verdict).
_CRITIQUE_KEY = "critique"
_OMISSIONS_KEY = "security_abuse_omissions"
# The planner's contract fit findings ride the same block and block the same way.
_CONTRACT_KEY = "contract_findings"
_COVERAGE_KEY = "coverage_findings"


def _incomplete_steps(criterion: dict[str, Any]) -> list[str]:
    """Step kinds a structured scenario lacks; ``[]`` for a text-only criterion.

    A criterion is structured once any step key carries a value; then each kind
    needs at least one non-empty string step (a wrong type fails closed)."""
    if not any(criterion.get(k) for k in _SCENARIO_STEPS):
        return []
    return [
        k for k in _SCENARIO_STEPS
        if not (
            isinstance(v := criterion.get(k), list)
            and any(isinstance(step, str) and step.strip() for step in v)
        )
    ]


class SpecGate:
    """Structural criteria-completeness check over ``criteria.json``."""

    name = "spec"
    verdict_class = VerdictClass.CONFIG_EVALUABLE
    decision_deterministic = True
    execution_deterministic = False  # pure read of a config artifact

    def __init__(
        self,
        criteria_filename: str = "criteria.json",
        *,
        require_security_criterion: bool = True,
        seal_key: bytes | None = None,
    ) -> None:
        self._criteria_filename = criteria_filename
        self._require_security_criterion = require_security_criterion
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)

    # ---- zone A: raw read + parse only ------------------------------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        path = workdir / self._criteria_filename
        if not path.is_file():
            return observe(
                self.name,
                structured={"parse_ok": False, "error": f"missing {self._criteria_filename}"},
                seal_key=self._seal_key,
            )
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return observe(
                self.name,
                structured={"parse_ok": False, "error": f"unparseable criteria: {exc}"},
                findings=(f"unparseable {self._criteria_filename}: {exc}",),
                seal_key=self._seal_key,
            )
        # Descriptive (non-verdict) observations of the parsed criteria so a FAIL
        # is diagnosable (which criterion / which missing field / no abuse case).
        # These are raw observations — zone B (`decide`) alone turns them into the
        # verdict; the findings only EXPLAIN it.
        return observe(
            self.name,
            structured={"parse_ok": True, "doc": doc},
            findings=self._describe(doc),
            seal_key=self._seal_key,
        )

    # ---- zone B: pure completeness rules over the raw payload --------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if not obs.structured.get("parse_ok"):
            return GateStatus.FAIL  # a missing/defective criteria artifact fails Spec
        criteria = self._criteria_of(obs.structured.get("doc"))
        if criteria is None:
            return GateStatus.FAIL
        if not criteria:
            return GateStatus.FAIL  # an empty criteria set specifies nothing
        for c in criteria:
            if not isinstance(c, dict):
                return GateStatus.FAIL
            for field in _REQUIRED_FIELDS:
                v = c.get(field)
                if not isinstance(v, str) or not v.strip():
                    return GateStatus.FAIL
            if _incomplete_steps(c):
                return GateStatus.FAIL
        if self._require_security_criterion and not any(
            c.get("security_abuse") is True for c in criteria
        ):
            # Security/abuse-class coverage omission fails the gate (gov §1, §1b).
            return GateStatus.FAIL
        if self._critique_blocks(obs.structured.get("doc")):
            # The Criteria-Completeness Critic flagged an applicable security/abuse
            # omission (CriteriaCritique.security_abuse_omissions). Zone B re-derives
            # the FAIL from the critic's raw findings — the critic minted no verdict
            # (gov §1b / agents.md Layer 1; LEARNINGS L5).
            return GateStatus.FAIL
        return GateStatus.PASS

    def _describe(self, doc: Any) -> tuple[str, ...]:
        """Human-readable observations of the parsed criteria (NOT a verdict).

        Mirrors the fields ``decide`` checks so the findings name exactly why a
        FAIL happened — surfaced in the run summary for a halted build. Pure
        observation: zone B re-derives the verdict independently."""
        criteria = self._criteria_of(doc)
        if criteria is None:
            return ("criteria doc is neither a JSON list nor an object with a 'criteria' list",)
        if not criteria:
            return ("criteria set is empty — nothing is specified",)
        out: list[str] = []
        for i, c in enumerate(criteria):
            if not isinstance(c, dict):
                out.append(f"criterion #{i} is not an object")
                continue
            cid = str(c.get("id") or f"#{i}")
            missing = [
                f for f in _REQUIRED_FIELDS
                if not (isinstance(v := c.get(f), str) and v.strip())
            ]
            if missing:
                out.append(f"criterion {cid}: missing/empty {', '.join(missing)}")
            if steps := _incomplete_steps(c):
                out.append(f"criterion {cid}: scenario has no {'/'.join(steps)} step")
        if self._require_security_criterion and not any(
            isinstance(c, dict) and c.get("security_abuse") is True for c in criteria
        ):
            out.append(
                "no criterion flags security_abuse=true — security/abuse coverage "
                "is mandatory (gov §1b)"
            )
        if self._critique_blocks(doc):
            contract = self._findings(doc, _CONTRACT_KEY)
            coverage = self._findings(doc, _COVERAGE_KEY)
            if not (contract or coverage) or self._critique_blocks(doc, (_OMISSIONS_KEY,)):
                out.append("criteria-completeness critic flagged a security/abuse omission")
            # Verbatim: the planner's revision pass is handed these findings.
            out += [f"contract fit: {f}" for f in contract]
            out += [f"coverage: {f}" for f in coverage]
        return tuple(out) if out else (f"{len(criteria)} criteria, all structurally complete",)

    @staticmethod
    def _criteria_of(doc: Any) -> list[Any] | None:
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict) and isinstance(doc.get("criteria"), list):
            return doc["criteria"]
        return None

    @staticmethod
    def _findings(doc: Any, key: str) -> list[str]:
        critique = doc.get(_CRITIQUE_KEY) if isinstance(doc, dict) else None
        found = critique.get(key) if isinstance(critique, dict) else None
        if not isinstance(found, list):
            return []
        return [f for f in found if isinstance(f, str) and f.strip()]

    @staticmethod
    def _critique_blocks(
        doc: Any, keys: tuple[str, ...] = (_OMISSIONS_KEY, _CONTRACT_KEY, _COVERAGE_KEY)
    ) -> bool:
        """True iff the materialized critique block flags a security/abuse omission,
        a contract fit finding or a coverage finding.

        Mirrors ``CriteriaCritique.spec_gate_blocking()`` over the RAW findings the
        materializer wrote: a non-empty ``security_abuse_omissions`` or
        ``contract_findings`` / ``coverage_findings`` list blocks.
        The block is OPTIONAL — its absence is "no flagged omission", never a PASS
        bit an agent can assert. A malformed block (wrong types) is treated as a
        present-but-uninterpretable finding and fails closed.
        """
        if not isinstance(doc, dict):
            return False  # bare-list doc carries no critique block
        critique = doc.get(_CRITIQUE_KEY)
        if critique is None:
            return False
        if not isinstance(critique, dict):
            return True  # present but unparseable → fail closed
        for key in keys:
            found = critique.get(key)
            if found is None:
                continue
            if not isinstance(found, list):
                return True  # present but unparseable → fail closed
            if any(isinstance(o, str) and o.strip() for o in found):
                return True
        return False
