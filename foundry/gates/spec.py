"""Spec gate — class 1 (config-evaluable). (governance §1, plan §5.1)

Evaluates the acceptance-criteria document deterministically: every criterion
must carry an id, testable text, and an oracle hint, and **security/abuse
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

# The critique block the materializer writes onto the criteria document from the
# Criteria-Completeness Critic's findings. Zone A surfaces the RAW omission list;
# zone B alone turns a non-empty list into a FAIL (the critic mints no verdict).
_CRITIQUE_KEY = "critique"
_OMISSIONS_KEY = "security_abuse_omissions"


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
    ) -> None:
        self._criteria_filename = criteria_filename
        self._require_security_criterion = require_security_criterion

    # ---- zone A: raw read + parse only ------------------------------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        path = workdir / self._criteria_filename
        if not path.is_file():
            return observe(
                self.name,
                structured={"parse_ok": False, "error": f"missing {self._criteria_filename}"},
            )
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return observe(
                self.name,
                structured={"parse_ok": False, "error": f"unparseable criteria: {exc}"},
                findings=(f"unparseable {self._criteria_filename}: {exc}",),
            )
        # Descriptive (non-verdict) observations of the parsed criteria so a FAIL
        # is diagnosable (which criterion / which missing field / no abuse case).
        # These are raw observations — zone B (`decide`) alone turns them into the
        # verdict; the findings only EXPLAIN it.
        return observe(
            self.name,
            structured={"parse_ok": True, "doc": doc},
            findings=self._describe(doc),
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
        if self._require_security_criterion and not any(
            isinstance(c, dict) and c.get("security_abuse") is True for c in criteria
        ):
            out.append(
                "no criterion flags security_abuse=true — security/abuse coverage "
                "is mandatory (gov §1b)"
            )
        if self._critique_blocks(doc):
            out.append("criteria-completeness critic flagged a security/abuse omission")
        return tuple(out) if out else (f"{len(criteria)} criteria, all structurally complete",)

    @staticmethod
    def _criteria_of(doc: Any) -> list[Any] | None:
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict) and isinstance(doc.get("criteria"), list):
            return doc["criteria"]
        return None

    @staticmethod
    def _critique_blocks(doc: Any) -> bool:
        """True iff the materialized critique block flags a security/abuse omission.

        Mirrors ``CriteriaCritique.spec_gate_blocking()`` over the RAW findings the
        materializer wrote: a non-empty ``security_abuse_omissions`` list blocks.
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
        omissions = critique.get(_OMISSIONS_KEY)
        if omissions is None:
            return False
        if not isinstance(omissions, list):
            return True  # present but unparseable → fail closed
        return any(isinstance(o, str) and o.strip() for o in omissions)
