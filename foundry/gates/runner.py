"""Two-zone deterministic gate runner. (governance §1c, implementation-plan §3.1)

The verdict bit for a deterministic gate is never an agent claim — and never
the product of one process that both *executes agent-influenced code* and
*holds a signing key*. The runner is split:

- **Zone A — untrusted executor.** Runs the gate's machine grounding
  (subprocess / pure-python evaluation over the workdir). It holds **no
  signing credential** — nothing in this module hands the gate impl the
  signer — and emits only a :class:`ZoneAObservation`: raw exit codes,
  captured output, structured findings, plus a ``results_digest``
  (content digest of the raw results) that the attestation tuple binds.
- **Zone B — trusted attestor.** Receives only zone A's raw observation,
  computes the verdict via the gate's **pure decision function**
  ``decide(obs) -> GateStatus``, builds the :class:`GateResult`, and signs it
  with the :class:`~foundry.gates.attestation.ZoneBSigner`. Zone B executes
  no agent code.

Agent code never touches either zone: agents write ``GateTriage`` (advisory),
and ``merge_gates`` drops any deterministic-gate write that does not carry a
valid zone-B attestation (foundry/state/reducers.py).

Infra failure is NOT a verdict (DS7): an exception escaping zone A — a
missing grounding tool, a sandbox crash, a timeout — produces a **signed
ERROR record**, never PASS (fail-closed) and never FAIL (it is not a judgment
about the tree; ERROR routes to bounded per-branch retry and **never latches**).

Stable-FAIL latch (governance §5): a FAIL recorded against an **integrated
digest** (``run_against`` is a tree/state sha, not a per-branch ``task/...``
ref) is latched in the GLOBAL cross-fork record set. The latch is
**monotonic** — a later PASS never auto-clears it — and **cross-fork**: it is
keyed on the digest, so a sibling fork (new ``run_id``) inherits the
disqualification and two forks that diverge on the same digest can never ship
the green one. Release requires an explicit, record-scoped override at
admission, outside this module.

Dev-grade caveat (implementation-plan §0.3): the *logic* of the split is real
(no key in zone A scope, observation-only seam, signed ERROR), but both zones
run in this process — production hardening puts zone A in a sandboxed
FS/network namespace with a sealed result channel. That is key/namespace
custody, not a change to this code's interfaces.

Forged-results hardening (T-M4-03, gov §1c/§4 — defends a Phase-4 adversarial
exit): a **sealed observation channel.** Zone A's :class:`ZoneAObservation` reaches
  zone B SEALED: zone A stamps an integrity tag (:func:`seal_observation`) with a
  sealing secret held in zone A's custody, and zone B re-derives the tag and
  rejects any mismatch. The seal covers the WHOLE observation (every raw field
  AND ``results_digest``), so an agent-influenced path cannot swap a FAIL
  observation for a PASS one between execution and decision — a tampered/forged
  observation is signed ERROR, never PASS. The seal is OPTIONAL (back-compat):
  with no sealing key the channel is unsealed exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from foundry.canonical import canonical_bytes, content_digest
from foundry.gates.attestation import ZoneBSigner
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.state.tree_sha import execution_reuse_key

__all__ = [
    "GateImpl",
    "GroundingUnavailable",
    "ObservationSealError",
    "TwoZoneGateRunner",
    "ZoneAObservation",
    "observe",
    "run_grounding_command",
    "seal_observation",
]

# An integrated run_against is a content digest (sha1/sha256 hex); anything
# else ("task/<id>" per-branch refs) is per-branch and never latches.
_DIGEST_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class GroundingUnavailable(RuntimeError):
    """The gate's machine grounding cannot run (missing tool/required input).

    Fail-closed: raised from zone A, it becomes a signed ERROR — never PASS.
    """


class ObservationSealError(RuntimeError):
    """The observation's seal does not verify against the zone-A sealing key.

    Fail-closed: a sealed channel that does not verify means the observation
    zone B is deciding on is NOT the one zone A produced (tampered/forged in
    transit) — it becomes a signed ERROR, never PASS.
    """


class ZoneAObservation(FrozenModel):
    """The ONLY thing zone A may hand zone B: raw machine observations.

    No verdict field exists here by design — zone A reports what happened
    (exit codes, captured output, structured findings); zone B alone decides.
    ``results_digest`` is the content digest of the raw results and is bound
    into the signed attestation tuple, so the observation the verdict was
    computed from cannot be swapped after signing.

    ``seal`` (optional) is an integrity tag stamped by zone A over the WHOLE
    observation with a sealing secret in zone A's custody. On a SEALED channel
    zone B re-derives the tag and rejects any mismatch, so a FAIL observation
    cannot be swapped for a PASS one between execution and decision. Empty when
    the channel is unsealed (back-compat).
    """

    gate_name: str
    exit_codes: tuple[int, ...] = ()
    stdout: str = ""
    stderr: str = ""
    findings: tuple[str, ...] = ()
    structured: dict[str, Any] = Field(default_factory=dict)
    results_digest: str
    seal: str = ""


def _observation_core(obs: ZoneAObservation) -> dict[str, Any]:
    """Canonical core of an observation — every field EXCEPT the seal itself.

    Covers ``results_digest`` so a swapped verdict (FAIL→PASS observation) breaks
    the tag, and the raw fields so the seal also fails if a body is rewritten to
    keep the digest internally consistent.
    """
    return {
        "kind": "zone_a_observation",
        "gate_name": obs.gate_name,
        "exit_codes": list(obs.exit_codes),
        "stdout": obs.stdout,
        "stderr": obs.stderr,
        "findings": list(obs.findings),
        "structured": dict(obs.structured),
        "results_digest": obs.results_digest,
    }


def seal_observation(obs: ZoneAObservation, seal_key: bytes) -> ZoneAObservation:
    """Stamp ``obs`` with a zone-A integrity tag (HMAC-SHA256 over its core).

    The sealing secret is in zone A's custody (dev-grade: a symmetric key, like
    the attestation root). Zone B holds the verification key and re-derives the
    same tag; the seal binds the WHOLE observation so a tampered/forged swap is
    detected. Returns a sealed copy (``ZoneAObservation`` is frozen).
    """
    tag = hmac.new(seal_key, canonical_bytes(_observation_core(obs)), hashlib.sha256).hexdigest()
    return obs.model_copy(update={"seal": f"seal256:{tag}"})


def _seal_intact(obs: ZoneAObservation, seal_key: bytes) -> bool:
    """Constant-time check that ``obs.seal`` matches a re-derived tag."""
    expected = seal_observation(obs.model_copy(update={"seal": ""}), seal_key).seal
    return bool(obs.seal) and hmac.compare_digest(expected.encode(), obs.seal.encode())


def observe(
    gate_name: str,
    *,
    exit_codes: tuple[int, ...] = (),
    stdout: str = "",
    stderr: str = "",
    findings: tuple[str, ...] = (),
    structured: dict[str, Any] | None = None,
    seal_key: bytes | None = None,
) -> ZoneAObservation:
    """Build a ZoneAObservation; results_digest = content digest of raw results.

    If ``seal_key`` is given the observation is SEALED with a zone-A integrity
    tag (:func:`seal_observation`) so a sealed-channel runner can detect a
    swapped/forged observation.
    """
    structured = dict(structured or {})
    digest = content_digest(
        {
            "kind": "zone_a_observation",
            "gate_name": gate_name,
            "exit_codes": list(exit_codes),
            "stdout": stdout,
            "stderr": stderr,
            "findings": list(findings),
            "structured": structured,
        }
    )
    obs = ZoneAObservation(
        gate_name=gate_name,
        exit_codes=exit_codes,
        stdout=stdout,
        stderr=stderr,
        findings=findings,
        structured=structured,
        results_digest=digest,
    )
    return seal_observation(obs, seal_key) if seal_key is not None else obs


def run_grounding_command(
    cmd: tuple[str, ...] | list[str],
    workdir: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 600.0,
) -> tuple[int, str, str]:
    """Zone-A subprocess grounding: run ``cmd`` in ``workdir``, observe rc/out/err.

    A missing binary raises :class:`GroundingUnavailable` (fail-closed →
    ERROR, never PASS). Timeouts propagate as exceptions → ERROR likewise.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise GroundingUnavailable(f"grounding tool not available: {cmd[0]!r}") from exc
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


class GateImpl(Protocol):
    """A deterministic gate implementation, split along the two-zone seam.

    ``execute`` is the zone-A machine grounding (runs in the untrusted
    executor, no signing key in scope); ``decide`` is the zone-B verdict — a
    PURE function of the observation, executing no agent code. Class-2
    (execution-deterministic) gates additionally expose ``mutant_rotation_id``
    and ``scanner_rule_version`` attributes for the reuse key (§1f).
    """

    name: str
    verdict_class: VerdictClass
    decision_deterministic: bool
    execution_deterministic: bool

    def execute(self, workdir: Path) -> ZoneAObservation: ...

    def decide(self, obs: ZoneAObservation) -> GateStatus: ...


def _is_integrated_digest(run_against: str) -> bool:
    return _DIGEST_RE.fullmatch(run_against) is not None


class TwoZoneGateRunner:
    """Executes gates in zone A, decides + signs in zone B. (gov §1c)

    Holds the :class:`ZoneBSigner` (zone B's identity) and the writable
    :class:`GlobalRecords` (for the stable-FAIL latch). Neither is ever
    passed into ``gate.execute`` — the zone-A call receives only the workdir,
    so no signing credential is in scope while agent-influenced code runs.
    """

    def __init__(
        self,
        signer: ZoneBSigner,
        records: GlobalRecords,
        scanner_config_rev: str,
        lineage_map_revision: str,
        *,
        seal_key: bytes | None = None,
    ) -> None:
        self._signer = signer
        self._records = records
        self._scanner_config_rev = scanner_config_rev
        self._lineage_map_revision = lineage_map_revision
        # Zone-A sealing key (verification side held by zone B). When present
        # the observation channel is SEALED — zone B rejects an unsealed or
        # mismatched observation. Absent → unsealed channel (back-compat).
        self._seal_key = seal_key

    @property
    def seal_key(self) -> bytes | None:
        """The zone-A observation-sealing key (or ``None`` on an unsealed channel).

        Exposed so the trusted gate-wiring can construct each :class:`GateImpl`
        with the SAME key the runner verifies against — zone A seals its own
        observation in its own custody, zone B (this runner) re-derives the tag.
        This is the seal (channel-integrity) key only; the signing credential is
        never exposed and never reaches a gate."""
        return self._seal_key

    @property
    def records(self) -> GlobalRecords:
        """The cross-fork :class:`GlobalRecords` (read-only view of the owner).

        Exposed so callers that need the stable-FAIL latch view (e.g. the
        supervisor's ``route_after_gates`` wiring) can read it WITHOUT reaching
        through ``_records``. Read-only access only — the runner remains the
        sole writer of the latch (``run`` calls ``latch_stable_fail``); this
        property exposes no mutation seam of its own.
        """
        return self._records

    def _verify_observation(self, obs: ZoneAObservation) -> None:
        """Zone-B integrity gate, run BEFORE ``decide``. Fail-closed.

        If a sealing key is held, the observation must carry a seal that
        re-derives — a tampered/forged swap raises :class:`ObservationSealError`
        (→ ERROR).
        """
        if self._seal_key is not None and not _seal_intact(obs, self._seal_key):
            raise ObservationSealError(
                "observation seal absent or does not verify — the decided "
                "observation is not the one zone A produced"
            )

    def run(self, gate: GateImpl, run_against: str, seq: Seq, workdir: Path | str) -> GateResult:
        """One gate run: zone-A grounding → zone-B verdict → sign → latch.

        Any exception escaping zone A (or the zone-B decision function) yields
        a **signed ERROR** record — infra failure is not a verdict (DS7), and
        it must still be an attested, foldable record so routing can retry
        per-branch without an unsigned write ever entering the channel.
        """
        findings: tuple[str, ...]
        try:  # ---- zone A: untrusted execution, no signing key in scope ----
            obs = gate.execute(Path(workdir))
        except Exception as exc:
            obs = observe(
                gate.name,
                structured={"zone_a_exception": f"{type(exc).__name__}: {exc}"},
            )
            status = GateStatus.ERROR
            findings = (f"zone-a infra failure: {type(exc).__name__}: {exc}",)
        else:  # ---- zone B: verify the seam, then decide over the observation --
            try:
                self._verify_observation(obs)
                status = gate.decide(obs)
                findings = obs.findings
            except ObservationSealError as exc:
                # Tampered/forged observation: not a judgment about the tree.
                status = GateStatus.ERROR
                findings = (*obs.findings, f"sealed-channel violation: {exc}")
            except Exception as exc:
                status = GateStatus.ERROR
                findings = (*obs.findings, f"zone-b decision failure: {type(exc).__name__}: {exc}")

        result = GateResult(
            name=gate.name,
            status=status,
            decision_deterministic=gate.decision_deterministic,
            execution_deterministic=gate.execution_deterministic,
            verdict_class=gate.verdict_class,
            scanner_config_rev=self._scanner_config_rev,
            lineage_map_revision=self._lineage_map_revision,
            results_digest=obs.results_digest,
            findings=findings,
            run_against=run_against,
            seq=seq,
        )
        # Zone B signs (gate_name, run_against, scanner_config_rev,
        # lineage_map_revision, status, results_digest) — input-bound, so the
        # attestation cannot be replayed onto another tree/config/observation.
        result = self._signer.with_attestation(result)

        # Stable-FAIL latch (gov §5): FAIL on an integrated digest is latched
        # in the GLOBAL cross-fork store — monotonic (a later PASS never
        # auto-clears it) and fork-inherited (keyed on the digest, not run_id).
        # ERROR never latches (infra, not a verdict); per-branch FAILs route
        # back to the responsible role without disqualifying any digest.
        if status is GateStatus.FAIL and _is_integrated_digest(run_against):
            self._records.latch_stable_fail(run_against, gate.name, seq.run_id)

        return result

    def reuse_key(self, gate: GateImpl, state_sha: str) -> str:
        """Dependency-scoped verdict-cache key for this gate at this state. (§1f)

        Execution-dependent (class-2) verdicts depend on inputs not in the
        tree — the held-out mutant rotation and the scanner rule version — so
        their reuse key binds ``mutant_rotation_id`` + ``scanner_rule_version``
        (the same values bound into the attestation tuple): a rotation or
        rule-version boundary invalidates the cached verdict instead of
        laundering it forward. Config-evaluable verdicts key on the state sha
        alone.
        """
        if gate.verdict_class is VerdictClass.EXECUTION_DETERMINISTIC:
            return execution_reuse_key(
                state_sha,
                gate.mutant_rotation_id,  # type: ignore[attr-defined]
                gate.scanner_rule_version,  # type: ignore[attr-defined]
            )
        return state_sha
