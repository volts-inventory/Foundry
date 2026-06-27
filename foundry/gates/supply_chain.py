"""Supply-chain gate — class 1 (config-evaluable). (governance §1/§4, plan §5.1)

Build/CI/IaC/Dockerfile/lockfile/scanner-config changes are high-privilege
artifacts — a malicious edit here subverts the trust root regardless of how
clean the application diff is — so they are policed separately from application
code (gov §4). This is a deterministic OPA-style policy over the *diff text*:

- **High-privilege path touched** (CI workflows, Dockerfiles, the build-system
  table of ``pyproject.toml``, IaC files) ⇒ FAIL unless ``allow_build_changes``
  is explicitly set for this run (the operator opt-in that a build/CI change is
  intended and separately reviewed).
- **New dependency source** (a URL/VCS/local ref introduced into a
  ``requirements*.txt`` line) ⇒ FAIL — a new package origin is a supply-chain
  decision, not an application change.

Pure-python and self-contained: the input is the unified-diff text, so the gate
needs no scanners and never executes the diff. ``scanner_config_rev`` in the
attestation pins the policy revision.
"""

from __future__ import annotations

import re
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import GroundingUnavailable, ZoneAObservation, observe

__all__ = ["HIGH_PRIVILEGE_PATTERNS", "SupplyChainGate"]

#: Paths whose change is a high-privilege supply-chain event (regex on the
#: diff's b-side path, posix). Matched anywhere in the path.
HIGH_PRIVILEGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ci-workflow", re.compile(r"(?:^|/)\.github/workflows/")),
    ("dockerfile", re.compile(r"(?:^|/)Dockerfile(?:\.|$)")),
    ("iac", re.compile(r"\.(?:tf|tfvars)$|(?:^|/)(?:k8s|kubernetes|helm|charts)/")),
    ("ci-config", re.compile(r"(?:^|/)(?:\.gitlab-ci\.yml|\.circleci/|Jenkinsfile)")),
)

# A new pyproject build-system table edit (b-side hunk introducing it).
_BUILD_SYSTEM = re.compile(r"^\+.*\[build-system\]", re.MULTILINE)
# diff b-side path header: "+++ b/<path>"
_DIFF_BPATH = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
# An added requirements line introducing a URL/VCS/local source.
_NEW_DEP_SOURCE = re.compile(
    r"^\+\s*(?:-e\s+)?(?:https?://|git\+|file:|\S+\s+@\s+(?:https?://|git\+|file:))",
    re.MULTILINE,
)


class SupplyChainGate:
    """Deterministic policy over a unified diff of build/CI/IaC artifacts."""

    name = "supply_chain"
    verdict_class = VerdictClass.CONFIG_EVALUABLE
    decision_deterministic = True
    execution_deterministic = False

    def __init__(
        self,
        *,
        diff_filename: str = "changes.diff",
        allow_build_changes: bool = False,
    ) -> None:
        self._diff_filename = diff_filename
        self._allow_build_changes = allow_build_changes

    # ---- zone A: read the diff text + collect raw policy hits ---------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        path = workdir / self._diff_filename
        if not path.is_file():
            # No diff to police is not the same as a clean diff — a missing
            # required input is fail-closed (ERROR via the decide guard below).
            raise GroundingUnavailable(f"missing diff artifact {self._diff_filename!r}")
        try:
            diff = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError) as exc:
            raise GroundingUnavailable(f"unreadable diff artifact: {exc}") from exc

        bpaths = _DIFF_BPATH.findall(diff)
        privilege_hits: list[str] = []
        for bpath in bpaths:
            for rule_id, pattern in HIGH_PRIVILEGE_PATTERNS:
                if pattern.search(bpath):
                    privilege_hits.append(f"{rule_id}: {bpath}")
            if bpath.endswith("pyproject.toml") and _BUILD_SYSTEM.search(diff):
                privilege_hits.append(f"build-system: {bpath}")

        new_sources = [m.strip() for m in _NEW_DEP_SOURCE.findall(diff)]
        return observe(
            self.name,
            findings=tuple(
                sorted({*privilege_hits, *(f"new-dep-source: {s}" for s in new_sources)})
            ),
            structured={
                "diff_present": True,
                "allow_build_changes": self._allow_build_changes,
                "privilege_hit_count": len(privilege_hits),
                "new_source_count": len(new_sources),
            },
        )

    # ---- zone B: any unauthorized high-privilege change ⇒ FAIL -------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if not obs.structured.get("diff_present"):
            return GateStatus.ERROR
        if not obs.findings:
            return GateStatus.PASS
        # A new dependency source always fails; high-privilege path/build edits
        # fail unless the run explicitly authorized build changes.
        new_source = any(f.startswith("new-dep-source:") for f in obs.findings)
        privilege = any(not f.startswith("new-dep-source:") for f in obs.findings)
        if new_source:
            return GateStatus.FAIL
        if privilege and not obs.structured.get("allow_build_changes"):
            return GateStatus.FAIL
        return GateStatus.PASS
