"""Security gate (deterministic part) — class 2. (governance §1/§1c, plan §5.1)

A pure-python secret-pattern scan (private-key blocks, AWS access keys,
hardcoded passwords/tokens) over the workdir's tracked text files — real
machine grounding, fail-closed. Any finding ⇒ FAIL (no unresolved
high/critical); the labeled scan ``mode`` in the observation is what makes a
verdict (a missing scan ⇒ ERROR, never PASS).

``scanner_rule_version`` binds the rule-set version into the class-2 reuse key
(§1f), so a rule bump invalidates cached verdicts.

Threat-model judgment residual is NOT here: items reducible to oracles bind
into this verdict; the rest go to the Threat-review judgment sub-gate
(governance §1d) and never enter the runner-signed predicate.
"""

from __future__ import annotations

import re
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.state.tree_sha import default_exclude

from .runner import ZoneAObservation, observe

__all__ = ["DEV_SECRET_RULES", "SecurityGate"]

#: Dev-grade secret rules: (rule_id, compiled pattern). Pinned by version below.
DEV_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "hardcoded-password",
        re.compile(
            r"""(?i)\b(?:password|passwd|secret|api_key|token)\s*[:=]\s*["'][^"']{4,}["']"""
        ),
    ),
)

_DEV_RULE_VERSION = "dev-secret-rules-v1"
_MAX_SCAN_BYTES = 1_000_000  # dev-grade cap; oversized files are skipped


class SecurityGate:
    """Deterministic SAST/secret scan; findings ⇒ FAIL; missing tool ⇒ ERROR."""

    name = "security"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(self, *, mutant_rotation_id: int = 0) -> None:
        self.mutant_rotation_id = mutant_rotation_id
        # Rule-set version bound into the class-2 reuse key (§1f) — fixed at
        # construction so reuse_key() is stable independent of execution.
        self.scanner_rule_version = _DEV_RULE_VERSION

    # ---- zone A: run the secret scan, observe raw findings ------------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        findings: list[str] = []
        scanned = 0
        for path in sorted(workdir.rglob("*")):
            rel = path.relative_to(workdir).as_posix()
            if not path.is_file() or default_exclude(rel):
                continue
            try:
                if path.stat().st_size > _MAX_SCAN_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue  # binary/unreadable: out of dev-scan scope
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                for rule_id, pattern in DEV_SECRET_RULES:
                    if pattern.search(line):
                        findings.append(f"{rel}:{lineno}: {rule_id}")
        return observe(
            self.name,
            findings=tuple(findings),
            structured={
                "mode": "dev-grade-secret-scan",  # labeled per plan §0.3
                "rule_version": _DEV_RULE_VERSION,
                "files_scanned": scanned,
            },
        )

    # ---- zone B: findings present ⇒ FAIL ------------------------------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if not obs.structured.get("mode"):
            return GateStatus.ERROR  # no grounded scan -> not a verdict
        return GateStatus.FAIL if obs.findings else GateStatus.PASS
