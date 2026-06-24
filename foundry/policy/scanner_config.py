"""Run-immutable, content-addressed scanner config. (governance §2, §1c, §4)

The Policy plane's load-bearing artifact. Scanner configuration — the SAST /
SCA / secret rules + their versions, plus severity floors and suppression
allowlists — lives in a **protected, repo-external, run-immutable** path that
agents cannot write (governance §2). Scanners run from this *pinned* config, so
an agent diff cannot weaken them, and a config swap is **detectable** because
the pinned revision hash is bound into every runner attestation
(``scanner_config_rev``, gates/runner.py) and into the SLSA predicate
(governance §4) and re-checked at admission (governance §5).

This module models that pin as a frozen, content-addressed record:

- :class:`ScannerConfigRecord` is a ``FrozenModel`` — once built it cannot be
  mutated in place (pydantic ``frozen=True``). Its identity is its **content
  address** (``revision_hash`` = ``content_digest`` over the canonical record),
  so any change to the rules/versions/floors/allowlists yields a DIFFERENT
  identity. A run binds to one ``revision_hash``; a tampered config can never
  masquerade as the pinned one.
- :class:`PinnedScannerConfig` is the **run binding**: it pairs the frozen
  record with the ``revision_hash`` the run was launched against and exposes
  ``verify()`` — fail-closed tamper detection. A run **cannot mutate its own
  scanner config**; the only sanctioned change is producing a NEW record (a new
  revision), which is a separate-trust-boundary, two-person, WORM-logged event
  in production (governance §2) — modeled here as constructing a new record.

DEV-GRADE caveat (implementation-plan §0.3): the external scanner-config repo,
its separate trust boundary, and the two-person change control are an OPS
deployment concern. The *logic* — content-addressed immutability, the pinned
revision the verdict binds to, fail-closed tamper detection — is real and is
what gate verdicts already bind to via ``scanner_config_rev``.
"""

from __future__ import annotations

from foundry.canonical import content_digest
from foundry.schemas.base import FrozenModel

__all__ = [
    "PinnedScannerConfig",
    "ScannerConfigRecord",
    "ScannerConfigTamperError",
    "ScannerRule",
]


class ScannerConfigTamperError(RuntimeError):
    """The pinned scanner config does not match its bound revision hash.

    Fail-closed: a run whose scanner config fails to verify against the pinned
    ``revision_hash`` must NOT proceed — a mismatch means the run-immutable
    config was tampered with (or a different config was substituted).
    """


class ScannerRule(FrozenModel):
    """One pinned scanner rule: an ``(id, family, version)`` triple.

    ``family`` is the scanner class the rule belongs to (``sast`` / ``sca`` /
    ``secret``). ``version`` pins the exact rule-set version actually used so a
    rule bump invalidates cached class-2 verdicts (the same value the Security
    gate binds as ``scanner_rule_version``; governance §1f).
    """

    id: str
    family: str  # "sast" | "sca" | "secret"
    version: str


class ScannerConfigRecord(FrozenModel):
    """A frozen, content-addressed snapshot of the scanner configuration.

    Immutable by construction (``FrozenModel`` → pydantic ``frozen=True``): a
    run cannot mutate its own scanner config. Its ``revision_hash`` is the
    content address over the canonical record, so any rule/version/floor/
    allowlist change produces a different identity (detectable).
    """

    rules: tuple[ScannerRule, ...]
    # Severity floor per scanner family (e.g. {"sast": "high"}). A weakened
    # floor changes the content address — it cannot be slipped in unnoticed.
    severity_floors: tuple[tuple[str, str], ...] = ()
    # Suppression allowlist (`.semgrepignore` / `# nosec` / `.trivyignore`
    # equivalents). Run-immutable: a new suppression is a new revision, not an
    # in-place edit (governance §2 routes it to HITL as a first-class finding).
    suppression_allowlist: tuple[str, ...] = ()
    # License allowlist for the Audit/SBOM gate.
    license_allowlist: tuple[str, ...] = ()
    # The external trust-boundary source identity (repo + its own pinned rev).
    source_ref: str = "dev-local-scanner-config"

    @property
    def revision_hash(self) -> str:
        """Content address of the canonical record — the pinned identity.

        This is the value bound into runner attestations as
        ``scanner_config_rev`` and into the SLSA predicate; admission re-checks
        it (governance §4–§5).
        """
        return content_digest(self.model_dump(mode="json"))

    def rule_versions(self) -> tuple[str, ...]:
        """Sorted distinct rule versions — the class-2 reuse-key input (§1f)."""
        return tuple(sorted({r.version for r in self.rules}))


class PinnedScannerConfig(FrozenModel):
    """A run's binding to ONE scanner-config revision. (governance §2)

    Pairs the frozen :class:`ScannerConfigRecord` with the ``revision_hash``
    the run was pinned to. ``verify()`` is fail-closed tamper detection: it
    recomputes the record's content address and rejects any mismatch.
    """

    record: ScannerConfigRecord
    pinned_revision: str

    @classmethod
    def pin(cls, record: ScannerConfigRecord) -> PinnedScannerConfig:
        """Pin a run to ``record`` at its current content address."""
        return cls(record=record, pinned_revision=record.revision_hash)

    def is_intact(self) -> bool:
        """True iff the record still hashes to the pinned revision."""
        return self.record.revision_hash == self.pinned_revision

    def verify(self) -> str:
        """Return the pinned revision, or raise if the config was tampered with.

        Fail-closed: the only success path is an exact content-address match.
        """
        if not self.is_intact():
            raise ScannerConfigTamperError(
                f"scanner config tampered: record hashes to "
                f"{self.record.revision_hash!r}, pinned {self.pinned_revision!r}"
            )
        return self.pinned_revision
