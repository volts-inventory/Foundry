"""Oracle / gate / criteria / scanner-config diff monitor — the CRITICAL-DIFF
trip. (governance.md §3 hard rule; SEC3, AI10, SEC2)

ANY diff touching test oracles, gate logic, acceptance criteria, or scanner
config is a **deterministic HITL trip — no judgment, no override**. This
module intentionally exposes no waiver, allowlist-bypass, or severity knob:
the only configuration is *which paths count as protected*, and matching any
of them always trips. The sandboxed analyzer may annotate such a diff with
semantic color, but the trip stands regardless (see guardian.py).

Glob matching is implemented locally (``**``-aware) so the rule is a pure
function of (globs, paths) — deterministic, no LLM (gov §3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from foundry.monitors import Trip, TripKind
from foundry.schemas.base import FrozenModel

DEFAULT_ORACLE_GLOBS: tuple[str, ...] = ("tests/**", "**/conftest.py")
DEFAULT_GATE_GLOBS: tuple[str, ...] = ("foundry/gates/**",)
DEFAULT_CRITERIA_GLOBS: tuple[str, ...] = (
    "**/acceptance_criteria/**",
    "**/*.criteria.yaml",
    "**/*.criteria.json",
)
DEFAULT_SCANNER_CONFIG_GLOBS: tuple[str, ...] = (
    "**/.semgrep.yml",
    "**/.semgrep/**",
    "**/scanner_config*",
    "**/codeql/**",
)


class DiffEntry(FrozenModel):
    """One changed file in a diff: path + (optional) hunks. Matching is on
    the path — a touched protected file trips regardless of hunk content."""

    path: str
    hunks: tuple[str, ...] = ()


class RuleMatch(FrozenModel):
    rule: str
    path: str


class CriticalDiffTrip(Trip):
    """Typed trip record naming the matched rule(s) + path(s)."""

    matches: tuple[RuleMatch, ...]


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a `**`-aware posix glob into a full-match regex.

    `**/` matches zero or more directories; `*` never crosses `/`.
    """
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if glob[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


class ProtectedPathRule:
    """A named, compiled set of protected-path globs."""

    def __init__(self, name: str, globs: Iterable[str]) -> None:
        self.name = name
        self.globs = tuple(globs)
        self._patterns = tuple(_glob_to_regex(g) for g in self.globs)

    def matches(self, path: str) -> bool:
        normalized = path.lstrip("./")
        return any(p.match(normalized) for p in self._patterns)


class OracleMonitor:
    """Deterministic critical-diff monitor (gov §3 hard rule)."""

    def __init__(
        self,
        *,
        oracle_globs: Iterable[str] = DEFAULT_ORACLE_GLOBS,
        gate_globs: Iterable[str] = DEFAULT_GATE_GLOBS,
        criteria_globs: Iterable[str] = DEFAULT_CRITERIA_GLOBS,
        scanner_config_globs: Iterable[str] = DEFAULT_SCANNER_CONFIG_GLOBS,
    ) -> None:
        self.rules: tuple[ProtectedPathRule, ...] = (
            ProtectedPathRule("test-oracle", oracle_globs),
            ProtectedPathRule("gate-logic", gate_globs),
            ProtectedPathRule("acceptance-criteria", criteria_globs),
            ProtectedPathRule("scanner-config", scanner_config_globs),
        )

    def inspect(self, diff: Sequence[DiffEntry | str]) -> CriticalDiffTrip | None:
        """Return the deterministic trip if ANY changed path is protected.

        There is deliberately no severity, no override hook, no allow-path:
        a match forces HITL (Guardian maps CRITICAL_DIFF → ESCALATE).
        """
        matches: list[RuleMatch] = []
        for entry in diff:
            path = entry if isinstance(entry, str) else entry.path
            for rule in self.rules:
                if rule.matches(path):
                    matches.append(RuleMatch(rule=rule.name, path=path))
        if not matches:
            return None
        rule_names = sorted({m.rule for m in matches})
        paths = sorted({m.path for m in matches})
        return CriticalDiffTrip(
            monitor="oracle_monitor",
            kind=TripKind.CRITICAL_DIFF,
            rule=",".join(rule_names),
            reason=(
                "diff touches protected oracle/gate/criteria/scanner-config "
                f"paths ({', '.join(rule_names)}): deterministic HITL trip (gov §3)"
            ),
            matched=tuple(paths),
            matches=tuple(matches),
        )
