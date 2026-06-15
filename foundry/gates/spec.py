"""Spec gate — class 1 (config-evaluable). (governance §1, plan §5.1)

Evaluates the acceptance-criteria document deterministically: every criterion
must carry an id, testable text, and an oracle hint, and **security/abuse
criteria coverage is mandatory where applicable — omission fails the gate**
(governance §1b: "100% criterion-coverage" cannot be satisfied by
under-specifying).

Two-zone split: zone A (``execute``) only reads + parses the criteria
document and reports the raw parsed payload; zone B (``decide``) applies the
completeness rules — the verdict is a pure function of the observation, never
a bit the document (an agent-authored artifact) self-reports.

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
            )
        return observe(self.name, structured={"parse_ok": True, "doc": doc})

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
        return GateStatus.PASS

    @staticmethod
    def _criteria_of(doc: Any) -> list[Any] | None:
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict) and isinstance(doc.get("criteria"), list):
            return doc["criteria"]
        return None
