"""Security gate (deterministic part) — class 2. (governance §1/§1c, plan §5.1)

Grounding modes, fail-closed in both:

- **semgrep mode** (when the pinned scanner binary is available): zone A runs
  semgrep against a **pinned rules directory** — never agent-supplied rules —
  and observes the structured findings JSON. A selected-but-broken scanner or
  missing rules dir is ``GroundingUnavailable`` → signed ERROR, never PASS.
- **dev-grade fallback** (plan §0.3, explicitly labeled in the observation):
  a pure-python secret-pattern scan (private-key blocks, AWS access keys,
  hardcoded passwords/tokens) over the workdir's tracked text files. Real
  machine grounding, reduced rule coverage — production replaces the rule
  set, not the seam.

Any finding ⇒ FAIL (no unresolved high/critical). Production pins the full
SAST config revision externally (run-immutable scanner-config repo, gov §2);
that pin is already carried in every attestation as ``scanner_config_rev`` —
this module's ``scanner_rule_version`` additionally binds the *rule-set
version actually used* into the class-2 reuse key, so a rule bump invalidates
cached verdicts (§1f).

Threat-model judgment residual is NOT here: items reducible to oracles bind
into this verdict; the rest go to the Threat-review judgment sub-gate
(governance §1d) and never enter the runner-signed predicate.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.state.tree_sha import default_exclude

from .runner import GroundingUnavailable, ZoneAObservation, observe, run_grounding_command

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

    def __init__(
        self,
        *,
        use_semgrep: bool | None = None,
        semgrep_rules_dir: Path | str | None = None,
        mutant_rotation_id: int = 0,
        timeout_s: float = 600.0,
    ) -> None:
        # None -> auto-detect; False -> force the dev-grade scan (tests pin this).
        self._use_semgrep = use_semgrep
        self._semgrep_rules_dir = Path(semgrep_rules_dir) if semgrep_rules_dir else None
        self.mutant_rotation_id = mutant_rotation_id
        # Rule-set version bound into the class-2 reuse key (§1f) — fixed at
        # construction so reuse_key() is stable independent of execution.
        self.scanner_rule_version = (
            f"semgrep:{self._semgrep_rules_dir.name}"
            if self._semgrep_rules_dir is not None
            else _DEV_RULE_VERSION
        )
        self._timeout_s = timeout_s

    # ---- zone A: run the pinned scanner / dev scan, observe raw findings ----

    def execute(self, workdir: Path) -> ZoneAObservation:
        semgrep = None if self._use_semgrep is False else shutil.which("semgrep")
        if self._use_semgrep is True and semgrep is None:
            raise GroundingUnavailable("semgrep required but not on PATH")
        if semgrep is not None:
            return self._execute_semgrep(semgrep, workdir)
        return self._execute_dev_scan(workdir)

    def _execute_semgrep(self, semgrep: str, workdir: Path) -> ZoneAObservation:
        if self._semgrep_rules_dir is None or not self._semgrep_rules_dir.is_dir():
            # Pinned rules are mandatory — scanning with ambient/registry rules
            # would un-pin the config the attestation claims (gov §2).
            raise GroundingUnavailable("pinned semgrep rules dir missing")
        rc, out, err = run_grounding_command(
            (semgrep, "scan", "--json", "--quiet", "--config", str(self._semgrep_rules_dir), "."),
            workdir,
            timeout_s=self._timeout_s,
        )
        try:
            results = json.loads(out).get("results", [])
        except json.JSONDecodeError as exc:
            raise GroundingUnavailable(f"semgrep emitted unparseable output: {exc}") from exc
        findings = tuple(
            f"{r.get('path', '?')}:{r.get('start', {}).get('line', '?')}: {r.get('check_id', '?')}"
            for r in results
        )
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            findings=findings,
            structured={"mode": "semgrep", "rules": str(self._semgrep_rules_dir)},
        )

    def _execute_dev_scan(self, workdir: Path) -> ZoneAObservation:
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
