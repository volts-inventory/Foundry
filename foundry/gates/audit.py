"""Audit gate — class 1 (config-evaluable). (governance §1, plan §5.1)

Zone-B-rederived dependency inventory: the verdict is grounded in an SBOM the
trusted side reconstructs from the workdir's lockfiles — never an SBOM an
agent hands in. Two pure checks over that inventory:

- **Inventory derivable.** At least one recognized lockfile
  (``uv.lock`` / ``requirements*.txt`` / ``poetry.lock`` / ``package-lock.json``)
  must be present and readable; otherwise the inventory cannot be rederived and
  the gate is ERROR (fail-closed — a missing grounding input is not a PASS, and
  not a verdict about the tree either).
- **License allowlist.** No dependency may carry a license marker outside the
  pinned allowlist; a denied-license marker ⇒ FAIL.

The SBOM here is the content digest of the concatenated, canonically ordered
lockfile bytes (a stable per-tree identity); ``scanner_config_rev`` (carried in
every attestation) pins the license config.
"""

from __future__ import annotations

import re
from pathlib import Path

from foundry.canonical import content_digest
from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe

__all__ = ["DEFAULT_LICENSE_ALLOWLIST", "AuditGate", "recognized_manifests"]

#: Pinned, permissive-license allowlist (SPDX-style identifiers, lowercased).
DEFAULT_LICENSE_ALLOWLIST: frozenset[str] = frozenset(
    {"mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc", "psf", "python-2.0", "0bsd"}
)

#: Lockfiles / dependency manifests the inventory is rederived from, in canonical
#: order. Kept stack-agnostic so a non-Python/Node build (Go, Rust, JVM, Ruby, PHP)
#: can ground its SBOM too — otherwise the gate fail-closes to ERROR on those stacks
#: for want of a recognized manifest. Presence grounds the inventory; the license
#: marker scan below is best-effort (files without inline markers simply add no
#: findings). ``requirements*.txt`` is matched by glob (see ``_REQUIREMENTS_GLOB``).
_LOCKFILE_NAMES: tuple[str, ...] = (
    # Python
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    # Node
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    # Go (go.mod always present; go.sum only once there are deps)
    "go.mod",
    "go.sum",
    # Rust
    "Cargo.lock",
    # JVM
    "pom.xml",
    "gradle.lockfile",
    # Ruby / PHP
    "Gemfile.lock",
    "composer.lock",
)
_REQUIREMENTS_GLOB = "requirements*.txt"
_MAX_LOCKFILE_BYTES = 5_000_000


def recognized_manifests(workdir: Path) -> list[str]:
    """Names of the recognized dependency manifests present at ``workdir`` root.

    The single source of truth for "is there a manifest the audit can ground its
    SBOM in": a fixed lockfile name (:data:`_LOCKFILE_NAMES`) or a
    ``requirements*.txt`` glob match, ROOT ONLY (a vendored lockfile deep in the
    tree is not the project's manifest). Both :class:`AuditGate` (to decide
    derivability) and the trusted-side materializer's manifest scaffold read
    through THIS function, so the scaffold writes one iff the gate would have
    ERRORed for want of it.
    """
    names = [n for n in _LOCKFILE_NAMES if (workdir / n).is_file()]
    names += [p.name for p in workdir.glob(_REQUIREMENTS_GLOB) if p.is_file()]
    return names

# A license marker line: "license: <id>" / "License-Expression: <id>" etc.
_LICENSE_MARKER = re.compile(
    r"(?i)\blicense(?:[-_ ]expression)?\b\s*[:=]\s*['\"]?([A-Za-z0-9.\- ]+)"
)


class AuditGate:
    """Rederives a dependency inventory + checks licenses against a pinned allowlist."""

    name = "audit"
    verdict_class = VerdictClass.CONFIG_EVALUABLE
    decision_deterministic = True
    execution_deterministic = False  # pure read of pinned config artifacts

    def __init__(
        self,
        *,
        license_allowlist: frozenset[str] | None = None,
        seal_key: bytes | None = None,
    ) -> None:
        self._allowlist = license_allowlist or DEFAULT_LICENSE_ALLOWLIST
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)

    # ---- zone A: collect lockfile bytes + raw license markers --------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        lockfiles: list[str] = []
        blobs: list[str] = []
        denied: list[str] = []
        for name in sorted(self._lockfile_paths(workdir)):
            path = workdir / name
            try:
                if path.stat().st_size > _MAX_LOCKFILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            lockfiles.append(name)
            blobs.append(content_digest({"name": name, "body": text}))
            for marker in _LICENSE_MARKER.findall(text):
                license_id = marker.strip().lower()
                if license_id and license_id not in self._allowlist:
                    denied.append(f"{name}: {marker.strip()}")
        sbom_digest = content_digest({"kind": "sbom", "lockfiles": sorted(blobs)}) if blobs else ""
        return observe(
            self.name,
            findings=tuple(sorted(denied)),
            structured={
                "lockfiles": sorted(lockfiles),
                "inventory_derivable": bool(lockfiles),
                "sbom_digest": sbom_digest,
            },
            seal_key=self._seal_key,
        )

    def _lockfile_paths(self, workdir: Path) -> list[str]:
        return recognized_manifests(workdir)

    # ---- zone B: derivable + no denied license ⇒ PASS ----------------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if not obs.structured.get("inventory_derivable"):
            return GateStatus.ERROR  # no lockfile -> inventory not rederivable
        return GateStatus.FAIL if obs.findings else GateStatus.PASS
