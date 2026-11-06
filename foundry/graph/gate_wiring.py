r"""Graph-side gate wiring — run the deterministic gates at phase boundaries.

(governance §1c, architecture.md §3/§4/§7, implementation-plan §3.1)

This is the TRUST SEAM on the graph side: it materializes the integrated tree
from the content-addressed artifacts on ``state``, runs the Spec / Build / Test
gates through the :class:`~foundry.gates.runner.TwoZoneGateRunner` at a phase
boundary, and folds the runner-ATTESTED verdicts onto the ``gates`` channel.

What this module is careful about (the invariants the next waves reuse):

* **Verdicts come ONLY from the runner (LEARNINGS L5).** Nothing here ever
  constructs a :class:`GateResult` — the runner executes zone A, decides +
  signs in zone B, and returns an attested record. ``merge_gates`` admits a
  deterministic-gate write iff it carries a valid zone-B attestation, so an
  unsigned ``GateResult`` is dropped fail-closed. We only ever forward what the
  runner handed back.

* **Fail-closed materialization.** ``materialize_workdir`` writes the
  integrated tree the gates run against. The Spec gate's ``criteria.json`` is
  written from the AcceptanceCriterion artifacts; the code + test sources come
  from the FeatureDiff artifacts via the injected ``resolve`` (the SAME
  ``ArtifactResolver`` seam senior_engineer / qa use, never a parallel one). A
  handle present on state but unresolvable is an ERROR we RAISE (fail-closed) —
  the boundary must never silently run the gates against a partial tree and
  mint a vacuous PASS.

* **Integrated digest is the ``run_against``.** The materialized tree's
  ``tree_sha`` is the integrated digest the verdict binds + (on FAIL) latches
  against — never a per-branch ``task/<id>`` ref. (architecture.md §4)

* **Routing is fail-closed + convergence-bounded.** ``route_after_gates`` reads
  the boundary's relevant verdicts off ``state["gates"]`` and returns one of
  ``advance`` / ``fix`` / ``halt``. A stable-FAIL latch (committed cross-fork)
  is terminal → ``halt``; an exhausted ``fix_attempts`` bound → ``halt``; an
  ERROR (infra, not a verdict — DS7) routes to ``fix`` (bounded retry, never a
  latch); a flake-quarantine ERROR (N=2 split) does NOT fail the boundary.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import shlex
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata as _im
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.agents._runtime import AgentRuntime
from foundry.base_tree import base_features, copy_base_tree
from foundry.canonical import content_digest
from foundry.gates.build import BuildGate
from foundry.gates.pydeps import SCAFFOLD_HEADER, PythonBuildEnv, python_build_env
from foundry.gates.sandbox import toolchain_scope
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus, GateTriage, VerdictClass
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    FEATURE_DIR,
    FeatureDiff,
    StackDecision,
    Ticket,
    feature_path,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.reducers import merge_gates
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import GateImpl, TwoZoneGateRunner, ZoneAObservation
    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

    # The SAME resolver seam senior_engineer uses (LEARNINGS QS3 / IM4):
    # reconstitute a TYPED out-of-band body from its content-addressed Artifact
    # handle (AcceptanceCriterion / FeatureDiff) -> its FrozenModel. The return
    # is ``Any`` (mirrors senior_engineer.ArtifactResolver) but here we only ever
    # resolve typed bodies through it — the raw diff BODY rides a SEPARATE seam.
    ArtifactResolver = Callable[[Artifact], Any]

    # The DISTINCT blob-resolve seam keyed on the ``cas://`` diff_uri (LEARNINGS
    # D-R1-01/02). A FeatureDiff's diff body is stored out-of-band as a raw blob
    # via ``sandbox.put_blob`` (a UNIFIED-DIFF string) and read back via
    # ``sandbox.get_blob(uri) -> bytes``. We resolve it through THIS seam — keyed
    # on the uri, not the typed Artifact handle — so a kind-aware CAS resolver
    # cannot mis-dispatch the diff body as a typed artifact. The factory_graph
    # wave backs it with ``sandbox.get_blob``.
    BlobResolver = Callable[[str], "bytes | str"]

__all__ = [
    "BUILD_BOUNDARY",
    "BUILD_TEST_SMOKE_BOUNDARY",
    "CRITERIA_CRITIQUE_KIND",
    "CRITERION_KIND",
    "ESCALATE",
    "FEATURE_DIFF_KIND",
    "FeatureDiffBodyError",
    "MaterializationError",
    "PathContainmentError",
    "UnpinnedSmokeGate",
    "WaveBuildCheck",
    "bump_fix_attempts",
    "contract_paths",
    "failing_streak",
    "fix_cap",
    "flake_quarantined",
    "gate_profile",
    "materialize_workdir",
    "planned_paths",
    "render_feature",
    "route_after_gates",
    "run_build_gate",
    "run_build_test_gates",
    "run_spec_gate",
    "settle_fix_attempts",
    "unbuilt_manifests",
    "wave_typecheck",
]

# Artifact.kind tags the upstream layers stamp on their handles — the seam we
# read the boundary's DATA inputs back off the artifacts channel by.
CRITERION_KIND = "acceptance_criterion"  # product layer (LEARNINGS QS3)
TICKET_KIND = "ticket"
CRITERIA_CRITIQUE_KIND = "criteria_findings"  # Criteria-Completeness Critic output
FEATURE_DIFF_KIND = "feature_diff"  # implementation layer (junior_engineer)

# Dev-grade toolchain digest for tree_sha. Production threads the pinned builder
# image digest in; here a stable constant keeps the integrated digest a pure
# function of the materialized source (DS4).
_TOOLCHAIN_DIGEST = "foundry-dev-toolchain-v1"


class MaterializationError(RuntimeError):
    """A handle present on state could not be materialized into the workdir.

    Fail-closed: the boundary must never run the gates against a partial tree
    (that would risk a vacuous PASS, governance §1b). Raised when an artifact
    handle of a required kind resolves to the wrong type, or a FeatureDiff body
    cannot be turned into file content.
    """


class FeatureDiffBodyError(MaterializationError):
    """A resolved FeatureDiff diff body could not be applied to a worktree slice.

    The materialization contract: ``resolve(feature_diff_artifact)`` returns the
    typed :class:`FeatureDiff`; its ``diff_uri`` body — fetched through the
    SEPARATE :data:`BlobResolver` seam (``sandbox.get_blob``) — is a UNIFIED-DIFF
    string (what the StubAdapter / raw_react adapters emit via
    ``sandbox.put_blob``). It is parsed into a relative-POSIX-path -> file-content
    mapping by the Phase-1 additions-only applier. A body present but of a hunk
    shape we don't support yet (modifications / deletions) is fail-closed here
    rather than silently dropped.
    """


class PathContainmentError(MaterializationError):
    """A diff body names a path that escapes the integrated worktree.

    The diff body is agent-influenced content; a path like ``../x`` or ``/etc/x``
    would write OUTSIDE ``dest``. Rejected fail-closed BEFORE any write so the
    materialized tree the gates run against can only ever contain files inside it.
    """


# ---- artifact reads (by kind, newest-by-Seq) --------------------------------


def _of_kind(state: FactoryState, kind: str) -> list[Artifact]:
    return [a for a in (state.get("artifacts") or ()) if a.kind == kind]


def _resolve_typed(
    artifact: Artifact, resolve: ArtifactResolver, expected: type[Any], kind: str
) -> Any:
    """Reconstitute a typed body from its handle; fail-closed on type mismatch."""
    body = resolve(artifact)
    if not isinstance(body, expected):
        raise MaterializationError(
            f"resolved {kind!r} handle {artifact.uri!r} to {type(body).__name__}, "
            f"expected {expected.__name__}"
        )
    return body


# ---- criteria.json (the exact shape SpecGate parses) ------------------------


def _current_criteria(
    state: FactoryState, resolve: ArtifactResolver
) -> dict[str, AcceptanceCriterion]:
    """Newest record per criterion id, minus the scenarios of tickets a re-plan
    dropped (a ticket whose newest record supersedes its own id) and the ones a
    re-plan dropped from a ticket it kept (absent from the newest PRD's list):
    nothing builds those, so they must not be specified or executed."""
    from foundry.agents.quality._common import live_criterion_ids

    current: dict[str, AcceptanceCriterion] = {}
    for a in sorted(_of_kind(state, CRITERION_KIND), key=lambda a: a.seq):
        c = _resolve_typed(a, resolve, AcceptanceCriterion, CRITERION_KIND)
        current[c.id] = c
    live = live_criterion_ids(state, resolve)
    if live is not None:
        current = {cid: c for cid, c in current.items() if cid in live}
    if not any(c.ticket_id for c in current.values()):
        return current
    newest: dict[str, Ticket] = {}
    for a in sorted(_of_kind(state, TICKET_KIND), key=lambda a: a.seq):
        t = _resolve_typed(a, resolve, Ticket, TICKET_KIND)
        newest[t.id] = t
    retired = {tid for tid, t in newest.items() if t.supersedes == tid}
    return {cid: c for cid, c in current.items() if c.ticket_id not in retired}


def _criteria_list(
    state: FactoryState, resolve: ArtifactResolver
) -> list[dict[str, Any]]:
    """The acceptance-criteria array SpecGate parses, built from the artifacts.

    Each entry carries the fields SpecGate's zone-A read expects: ``id``,
    ``text``, ``oracle`` (mapped from :attr:`AcceptanceCriterion.oracle_hint`),
    and ``security_abuse``, plus ``given``/``when``/``then`` for a structured
    scenario (omitted for a text-only criterion, whose entry is unchanged).
    Ordered by criterion ``id`` so the materialized file
    — and thus the integrated ``tree_sha`` — is byte-stable across folds/resume.

    A re-plan re-emits criteria under the same ids; the newest-by-``Seq`` record
    per id is the current one. Taking it cannot weaken the gate: this gate
    re-validates exactly that newest set.
    """
    current = _current_criteria(state, resolve)
    out: list[dict[str, Any]] = []
    for c in sorted(current.values(), key=lambda c: c.id):
        entry: dict[str, Any] = {
            "id": c.id,
            "text": c.text,
            "oracle": c.oracle_hint,
            "security_abuse": c.security_abuse,
        }
        if c.given or c.when or c.then:
            entry.update(given=list(c.given), when=list(c.when), then=list(c.then))
        out.append(entry)
    return out


def _latest_critique(
    state: FactoryState, resolve: ArtifactResolver
) -> CriteriaCritique | None:
    """The newest Criteria-Completeness Critic finding on the channel, or ``None``.

    Resolved through the SAME typed ``resolve`` seam as the criteria (not a
    parallel one). Newest-by-``Seq`` so a supersession wins; ``None`` when the
    critic has not run (a degenerate greenfield boundary).
    """
    handles = _of_kind(state, CRITERIA_CRITIQUE_KIND)
    if not handles:
        return None
    latest = max(handles, key=lambda a: a.seq)
    return _resolve_typed(latest, resolve, CriteriaCritique, CRITERIA_CRITIQUE_KIND)


def _criteria_payload(
    state: FactoryState, resolve: ArtifactResolver
) -> list[dict[str, Any]] | dict[str, Any]:
    """The ``criteria.json`` document SpecGate parses, built from the artifacts.

    The document is the bare criteria LIST (byte-identical to the historical
    shape) UNLESS the Criteria-Completeness Critic flagged an applicable
    security/abuse omission. A flagged omission wraps the document as
    ``{"criteria": [...], "critique": {"security_abuse_omissions": [...]}}`` so
    the Spec gate's zone-A read materializes the critic's RAW findings as a gate
    input (SpecGate accepts both shapes). The critic mints NO verdict (LEARNINGS
    L5): the gate re-derives the FAIL from the non-empty omission list, so this
    is a zone-B-re-derivable input, not an agent-signed pass/fail bit.

    Why ONLY when blocking: an empty critique (the clean path) is materially the
    same input as "no critique" — both mean "nothing flagged". Emitting the wrapper
    only on a blocking finding keeps the clean tree BYTE-IDENTICAL to the historical
    list (so the integrated ``tree_sha`` is unperturbed and resume-stable), while a
    real omission still deterministically reshapes the document into a FAIL input.
    The document content is a pure function of the materialized findings either way.
    """
    criteria = _criteria_list(state, resolve)
    critique = _latest_critique(state, resolve)
    if critique is None or not critique.spec_gate_blocking():
        return criteria
    block: dict[str, list[str]] = {
        # RAW findings only — sorted for byte-stable tree_sha across folds.
        "security_abuse_omissions": sorted(critique.security_abuse_omissions),
    }
    if critique.contract_findings:
        block["contract_findings"] = sorted(critique.contract_findings)
    if critique.coverage_findings:
        block["coverage_findings"] = sorted(critique.coverage_findings)
    return {"criteria": criteria, "critique": block}


# ---- code + test sources from FeatureDiff diff bodies -----------------------

# Unified-diff line markers we recognise for the Phase-1 additions-only applier.
_DEV_NULL = "/dev/null"

# git-diff preamble lines that may precede a `--- /dev/null` addition section in a
# REAL unified diff (what `claude -p` / the OpenAI SDK emit). They carry no file
# CONTENT — a section marker, the addition mode, and blob hashes — so for an
# addition they are skipped. A modification/deletion/rename is NOT in this set and
# still fails closed at the `--- a/<path>` (non-/dev/null) check.
_GIT_PREAMBLE_PREFIXES = ("diff --git ", "index ", "new file mode ")


def _parse_addition_diff(diff_uri: str, body: str) -> dict[str, str]:
    """Parse a UNIFIED-DIFF body into a relative-path -> file-content mapping.

    Phase-1 scope: diffs are pure FILE ADDITIONS (the StubAdapter / raw_react
    adapters emit ``--- /dev/null`` / ``+++ b/<path>`` hunks). A
    minimal applier handling additions is sufficient here; FULL patch application
    (context-line matching, modifications, deletions, renames) is a LATER PHASE.
    We therefore FAIL-CLOSED (raise :class:`FeatureDiffBodyError`) on any hunk
    shape we don't support yet, rather than silently dropping content and letting
    a partial tree clear the gate (governance §1b).

    Recognised shape (one or more file sections):

        diff --git a/<path> b/<path>   # optional git preamble (real models emit it)
        new file mode 100644           #   (skipped — carries no content)
        index 000..abc                 #   (skipped)
        --- /dev/null
        +++ b/<relative/posix/path>
        @@ ... @@                 # hunk header (ranges, if present, are ignored)
        +<added line>             # every body line of an addition is '+'-prefixed
        ...

    Two producers round-trip through this: a REAL git-style unified diff (every
    added line ``+``-prefixed, optional ``diff --git``/``index``/``new file mode``
    preamble, ``\\ No newline at end of file`` markers — what ``claude -p`` / the
    OpenAI SDK emit), and the synthetic StubAdapter form, which ``+``-prefixes only
    the FIRST body line and appends the remaining file body verbatim (``@@\\n+<body>``).
    """
    files: dict[str, str] = {}
    lines = body.splitlines(keepends=True)
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.rstrip("\n")
        if not stripped.startswith("--- "):
            # Skip blank lines + the git-diff preamble that real producers emit
            # before a `--- /dev/null` addition section (these carry no content). A
            # MODIFICATION/deletion still fails closed: its `diff --git` is skipped
            # but the following `--- a/<path>` is not `/dev/null`, caught below.
            if stripped == "" or stripped.startswith(_GIT_PREAMBLE_PREFIXES):
                i += 1
                continue
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: unexpected line {stripped!r}; "
                f"the Phase-1 applier handles pure file-ADDITION hunks only"
            )
        old = stripped[len("--- ") :].strip()
        if old != _DEV_NULL:
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: '--- {old}' is a "
                f"modification/deletion; the Phase-1 applier handles ADDITIONS "
                f"('--- /dev/null') only"
            )
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: '--- /dev/null' not followed "
                f"by a '+++ b/<path>' header"
            )
        new = lines[i].rstrip("\n")[len("+++ ") :].strip()
        rel = new[2:] if new.startswith("b/") else new
        if not rel:
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: empty target path in '+++' header"
            )
        i += 1
        if i >= n or not lines[i].lstrip().startswith("@@"):
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: missing '@@' hunk header for {rel!r}"
            )
        i += 1
        # Collect the added body up to the next file section (next '--- ' or the
        # next git 'diff --git ' header in a multi-file diff).
        content_lines: list[str] = []
        first = True
        while i < n:
            line = lines[i]
            line_stripped = line.rstrip("\n")
            if line_stripped.startswith("--- ") or line_stripped.startswith("diff --git "):
                break
            if line.startswith("+"):
                content_lines.append(line[1:])
            elif line_stripped.startswith("\\"):
                # `\ No newline at end of file` — a real-diff marker, not content.
                pass
            elif first:
                # Defensive: a hunk body should open with a '+' line.
                raise FeatureDiffBodyError(
                    f"FeatureDiff body for {diff_uri!r}: hunk for {rel!r} does not "
                    f"begin with an added ('+') line — unsupported hunk shape"
                )
            else:
                # The StubAdapter prefixes only the FIRST body line with '+' and
                # appends the rest of the file content verbatim — take it as-is.
                content_lines.append(line)
            first = False
            i += 1
        files[rel] = "".join(content_lines)
    if not files:
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r} parsed to no files — empty/invalid diff"
        )
    return files


# ---- full patch application (ROADMAP Phase 2) --------------------------------
#
# The materialized tree is grown by applying each FeatureDiff's OPS in Seq order:
# whole-file WRITES (additions / snapshot slices), DELETES, and context-verified
# PATCHES to files earlier diffs contributed. Everything unrecognized stays
# fail-closed (governance §1b) — a hunk that cannot be applied EXACTLY raises,
# it is never fuzzily guessed (the gate fix loop retries with the failure).


@dataclass(frozen=True, slots=True)
class _Hunk:
    """One ``@@ -l[,n] +l[,n] @@`` hunk: (tag, line) pairs, tag ∈ {' ', '-', '+'}."""

    old_start: int  # 1-based line number in the ORIGINAL file
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _DiffOps:
    """Everything ONE FeatureDiff contributes to the integrated tree."""

    writes: dict[str, str]  # path -> full new content (additions / snapshot)
    deletes: tuple[str, ...] = ()  # paths removed from the tree
    patches: dict[str, tuple[_Hunk, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:  # dataclass default for a mutable field
        if self.patches is None:
            object.__setattr__(self, "patches", {})


def _diff_ops(diff: FeatureDiff, resolve_blob: BlobResolver) -> _DiffOps:
    """Parse a FeatureDiff body into its integration OPS.

    The diff BODY lives out-of-band behind ``diff.diff_uri`` (DS9) as a raw blob
    written by the adapter via ``sandbox.put_blob``, read back through the
    :data:`BlobResolver` seam. Three producer shapes round-trip:

    * a ``full-file-snapshot`` JSON body (agentic adapters): its ``files`` map is
      the write set verbatim and its optional ``deleted`` list is the delete set;
    * the additions-only unified diff (StubAdapter's synthetic shape + simple real
      diffs) — parsed by the LEGACY lenient parser first, byte-identical behavior;
    * a REAL unified diff with modification/deletion sections — parsed by the
      strict full parser (:func:`_parse_patch_diff`): additions become writes,
      ``+++ /dev/null`` sections become deletes, modification hunks become
      context-verified patches. Renames stay fail-closed (unsupported).
    """
    body = resolve_blob(diff.diff_uri)
    if isinstance(body, bytes):
        text = body.decode("utf-8")
    elif isinstance(body, str):
        text = body
    else:
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff.diff_uri!r} resolved to "
            f"{type(body).__name__}, expected the diff/snapshot blob (bytes|str)"
        )
    snapshot = _maybe_snapshot_files(diff.diff_uri, text)
    if snapshot is not None:
        return _DiffOps(writes=snapshot, deletes=_snapshot_deleted(diff.diff_uri, text))
    try:
        return _DiffOps(writes=_parse_addition_diff(diff.diff_uri, text))
    except FeatureDiffBodyError:
        # Not a pure-additions body: parse as a full patch (mods/deletes). If THIS
        # also rejects, its error names the real unsupported shape.
        return _parse_patch_diff(diff.diff_uri, text)


def _snapshot_deleted(diff_uri: str, text: str) -> tuple[str, ...]:
    """The optional ``deleted`` list of a full-file-snapshot body (fail-closed on
    a malformed shape; absent ⇒ empty)."""
    obj = json.loads(text)
    deleted = obj.get("deleted", [])
    if not isinstance(deleted, list) or not all(isinstance(p, str) for p in deleted):
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r}: full-file-snapshot 'deleted' must "
            f"be a list of paths"
        )
    return tuple(deleted)


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")

# Preamble lines a real `git diff` emits between file sections; they carry no
# content. `rename`/`copy` sections are NOT skippable — they change tree shape in
# a way this applier does not support, so they fail closed below.
_PATCH_PREAMBLE_PREFIXES = (
    "diff --git ", "index ", "new file mode ", "deleted file mode ",
    "old mode ", "new mode ",
)


def _parse_patch_diff(diff_uri: str, body: str) -> _DiffOps:
    """STRICT full unified-diff parser: additions, deletions, and modification
    hunks (context-verified at apply time). Fail-closed on renames/copies, missing
    headers, malformed hunks, or tags outside {' ', '+', '-', '\\'}."""
    writes: dict[str, str] = {}
    deletes: list[str] = []
    patches: dict[str, tuple[_Hunk, ...]] = {}
    lines = body.splitlines(keepends=True)
    i, n = 0, len(lines)
    saw_section = False
    while i < n:
        stripped = lines[i].rstrip("\n")
        if stripped == "" or stripped.startswith(_PATCH_PREAMBLE_PREFIXES):
            i += 1
            continue
        if stripped.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: rename/copy sections are not "
                f"supported by the applier — emit a delete + an addition instead"
            )
        if not stripped.startswith("--- "):
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: unexpected line {stripped!r} "
                f"between file sections"
            )
        old = stripped[len("--- ") :].strip()
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: '--- {old}' not followed by a "
                f"'+++' header"
            )
        new = lines[i].rstrip("\n")[len("+++ ") :].strip()
        i += 1
        old_rel = old[2:] if old.startswith("a/") else old
        new_rel = new[2:] if new.startswith("b/") else new
        saw_section = True

        hunks: list[_Hunk] = []
        while i < n and (m := _HUNK_HEADER_RE.match(lines[i].rstrip("\n"))):
            i += 1
            old_start = int(m.group(1))
            hunk_lines: list[tuple[str, str]] = []
            while i < n:
                line = lines[i]
                tag = line[0] if line.rstrip("\n") else " "
                if line.rstrip("\n").startswith(("--- ", "diff --git ")) or (
                    _HUNK_HEADER_RE.match(line.rstrip("\n"))
                ):
                    break
                if tag == "\\":  # `\ No newline at end of file`: previous line has no \n
                    if hunk_lines:
                        t, prev = hunk_lines[-1]
                        hunk_lines[-1] = (t, prev.rstrip("\n"))
                    i += 1
                    continue
                if tag not in (" ", "+", "-"):
                    raise FeatureDiffBodyError(
                        f"FeatureDiff body for {diff_uri!r}: unsupported hunk line "
                        f"tag {tag!r} in section for {new_rel or old_rel!r}"
                    )
                hunk_lines.append((tag, line[1:] if len(line) > 1 or tag != " " else ""))
                i += 1
            hunks.append(_Hunk(old_start=old_start, lines=tuple(hunk_lines)))

        if old == _DEV_NULL and new != _DEV_NULL:
            # Addition: the new file is exactly the '+' lines.
            content = "".join(t for tag, t in _all_hunk_lines(hunks) if tag == "+")
            writes[_require_rel(diff_uri, new_rel)] = content
        elif old != _DEV_NULL and new == _DEV_NULL:
            deletes.append(_require_rel(diff_uri, old_rel))
        elif old != _DEV_NULL and new != _DEV_NULL:
            if old_rel != new_rel:
                raise FeatureDiffBodyError(
                    f"FeatureDiff body for {diff_uri!r}: rename ({old_rel!r} -> "
                    f"{new_rel!r}) is not supported — emit a delete + an addition"
                )
            if not hunks:
                raise FeatureDiffBodyError(
                    f"FeatureDiff body for {diff_uri!r}: modification of {new_rel!r} "
                    f"has no hunks"
                )
            patches[_require_rel(diff_uri, new_rel)] = tuple(hunks)
        else:
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: '--- /dev/null' + "
                f"'+++ /dev/null' section is meaningless"
            )
    if not saw_section:
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r} parsed to no file sections — "
            f"empty/invalid diff"
        )
    return _DiffOps(writes=writes, deletes=tuple(deletes), patches=patches)


def _all_hunk_lines(hunks: list[_Hunk]) -> list[tuple[str, str]]:
    return [pair for h in hunks for pair in h.lines]


def _require_rel(diff_uri: str, rel: str) -> str:
    if not rel:
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r}: empty path in a file-section header"
        )
    return rel


def _apply_hunks(original: str, hunks: tuple[_Hunk, ...], *, diff_uri: str, rel: str) -> str:
    """Apply modification hunks to ``original`` with EXACT context verification.

    Strict + fail-closed: every context (' ') and removal ('-') line must match
    the original at the position the hunk header names — no fuzzing, no offset
    search. A mismatch raises :class:`FeatureDiffBodyError`; the gate fix loop
    (not a guess) is the recovery path. Hunks must be in ascending order.
    """
    src = original.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        start = hunk.old_start - 1
        if start < pos:
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: overlapping/unordered hunks "
                f"for {rel!r}"
            )
        out.extend(src[pos:start])
        pos = start
        for tag, text in hunk.lines:
            if tag == "+":
                out.append(text)
                continue
            if pos >= len(src) or src[pos].rstrip("\n") != text.rstrip("\n"):
                got = src[pos].rstrip("\n") if pos < len(src) else "<EOF>"
                raise FeatureDiffBodyError(
                    f"FeatureDiff body for {diff_uri!r}: hunk context mismatch in "
                    f"{rel!r} at line {pos + 1}: expected {text.rstrip(chr(10))!r}, "
                    f"found {got!r} — refusing to apply (fail-closed, no fuzz)"
                )
            if tag == " ":
                out.append(src[pos])
            pos += 1
    out.extend(src[pos:])
    return "".join(out)


def _maybe_snapshot_files(diff_uri: str, text: str) -> dict[str, str] | None:
    """Parse a ``full-file-snapshot`` body to its path->content map, else ``None``
    (the body is a unified diff). Fail-closed on a malformed snapshot shape."""
    if not text.lstrip().startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None  # a diff that merely happens to start with '{' — let the parser try
    if not (isinstance(obj, dict) and obj.get("kind") == "full-file-snapshot"):
        return None
    files = obj.get("files")
    if not isinstance(files, dict):
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r}: full-file-snapshot 'files' is "
            f"{type(files).__name__}, expected an object of path->content"
        )
    return {str(k): v if isinstance(v, str) else json.dumps(v) for k, v in files.items()}


def _contained_target(dest: Path, rel: str) -> Path:
    """Join ``rel`` under ``dest``, fail-closed on any traversal/escape.

    The diff body path is agent-influenced (D-R1-03): an absolute path, a path
    containing a ``..`` component, or one that otherwise resolves outside
    ``dest`` would let a write escape the integrated tree. Reject BEFORE writing.
    """
    pure = PurePosixPath(rel)
    if pure.is_absolute() or rel.startswith("/") or ".." in pure.parts:
        raise PathContainmentError(
            f"diff body path {rel!r} escapes the integrated tree "
            f"(absolute or '..' traversal) — rejected fail-closed"
        )
    dest_root = dest.resolve()
    target = (dest_root / pure).resolve()
    # Defense-in-depth: the resolved path must stay under dest_root.
    if target != dest_root and dest_root not in target.parents:
        raise PathContainmentError(
            f"diff body path {rel!r} resolves outside the integrated tree — "
            f"rejected fail-closed"
        )
    return target


def _write_sources(
    dest: Path, state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> None:
    """Materialize every FeatureDiff's OPS into ``dest`` (later-by-Seq wins).

    Diffs are applied in ``Seq`` order so a retried diff (higher epoch, L4)
    overwrites the predecessor it supersedes — the worktree reflects the
    integrated latest, deterministically and fold-order-independently. Each diff
    body is resolved through the :data:`BlobResolver` seam and parsed into ops
    (ROADMAP Phase 2: writes + deletes + context-verified patches); every target
    path is containment-checked (D-R1-03) before any write/unlink.

    * WRITE: whole-file content, parents created.
    * DELETE: unlink if present. Absent is NOT an error — a slice's delete may
      target a base-tree file that never entered the materialized overlay; the
      overlay semantic is "this path contributes nothing".
    * PATCH: the target must already exist in ``dest`` (written by a prior diff
      in Seq order) and every hunk must apply EXACTLY — else fail-closed.
    """
    _apply_diffs(dest, _of_kind(state, FEATURE_DIFF_KIND), resolve, resolve_blob)


def _apply_diffs(
    dest: Path,
    artifacts: list[Artifact],
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    *,
    only: frozenset[str] | None = None,
) -> None:
    """Apply ``artifacts`` in Seq order; ``only`` limits it to those paths."""
    for artifact in sorted(artifacts, key=lambda a: a.seq):
        diff = _resolve_typed(artifact, resolve, FeatureDiff, FEATURE_DIFF_KIND)
        ops = _diff_ops(diff, resolve_blob)
        for rel, content in ops.writes.items():
            if only is not None and rel not in only:
                continue
            target = _contained_target(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel in ops.deletes:
            if only is not None and rel not in only:
                continue
            target = _contained_target(dest, rel)
            if target.is_file():
                target.unlink()
        for rel, hunks in ops.patches.items():
            if only is not None and rel not in only:
                continue
            target = _contained_target(dest, rel)
            if not target.is_file():
                raise FeatureDiffBodyError(
                    f"FeatureDiff body for {diff.diff_uri!r}: modification hunks "
                    f"target {rel!r}, which is not in the integrated tree — "
                    f"refusing a blind patch (fail-closed)"
                )
            patched = _apply_hunks(
                target.read_text(encoding="utf-8"), hunks, diff_uri=diff.diff_uri, rel=rel
            )
            target.write_text(patched, encoding="utf-8")


# ---- executable BDD: features/<ticket>.feature from the criteria -------------


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_feature(ticket_id: str, criteria: list[AcceptanceCriterion]) -> str:
    """One ticket's Gherkin feature: a ``Scenario`` per criterion, NAMED by the
    criterion id so an executed scenario maps 1:1 back to its criterion. Ordered
    by id so the file (and the integrated ``tree_sha``) is byte-stable."""
    lines = [f"Feature: {_one_line(ticket_id)}", ""]
    for c in sorted(criteria, key=lambda c: c.id):
        lines.append(f"  Scenario: {_one_line(c.id)}")
        steps = [("Given", c.given), ("When", c.when), ("Then", c.then)]
        if not (c.given or c.when or c.then):
            # An unstructured criterion still needs a step to bind — an empty
            # scenario would pass vacuously.
            steps = [("Then", (c.text,))]
        for keyword, texts in steps:
            for i, text in enumerate(texts):
                lines.append(f"    {keyword if i == 0 else 'And'} {_one_line(text)}")
        lines.append("")
    return "\n".join(lines)


def _features(state: FactoryState, resolve: ArtifactResolver) -> dict[str, str]:
    """``features/<ticket>.feature`` path -> rendered body, for the newest record
    per criterion id that carries a ``ticket_id`` (criteria without one are skipped)."""
    current = _current_criteria(state, resolve)
    by_ticket: dict[str, list[AcceptanceCriterion]] = {}
    for c in current.values():
        if c.ticket_id:
            by_ticket.setdefault(c.ticket_id, []).append(c)
    out: dict[str, str] = {}
    for ticket_id in sorted(by_ticket):
        rel = feature_path(ticket_id)
        # ticket_id is planner (model) output: keep the file directly under features/.
        if PurePosixPath(rel).parent != PurePosixPath(FEATURE_DIR):
            raise PathContainmentError(
                f"ticket id {ticket_id!r} does not name a file directly under "
                f"{FEATURE_DIR}/ — rejected fail-closed"
            )
        out[rel] = render_feature(ticket_id, by_ticket[ticket_id])
    return out


def _write_features(dest: Path, features: dict[str, str], *, prune: bool = False) -> None:
    """Write the trusted feature files. With ``prune``, also drop any other
    ``features/*.feature`` an agent diff introduced: the directory is gate-owned,
    and the trusted copies overwrite any agent edit (agents see them, can't change them)."""
    if not features:
        return
    for rel, body in features.items():
        target = _contained_target(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    if prune:
        for stray in (dest / FEATURE_DIR).glob("*.feature"):
            if f"{FEATURE_DIR}/{stray.name}" not in features:
                stray.unlink()


# ---- dependency-manifest scaffold (audit-gate grounding) --------------------
#
# The dependency/license AUDIT gate grounds its SBOM in a recognized manifest at
# the repo root; with none it fail-closes to ERROR (a missing grounding INPUT,
# not a verdict about the tree). A greenfield build whose engineers did not
# author a manifest — common for a pure-stdlib Python component — would then
# ERROR for want of a file, regardless of what the user asked for. So the
# trusted side GUARANTEES the integrated tree ships an auditable manifest: when
# none is present, it writes one derived HONESTLY from the tree's own imports
# (never a fabricated dependency). An engineer-authored manifest is left
# untouched — the gate audits what they actually declared.


def _pin(dist: str) -> str:
    """``dist==version`` when the distribution is installed here, else the bare
    name (still an auditable manifest entry; the gate needs presence, not a
    resolvable pin)."""
    try:
        return f"{dist}=={_im.version(dist)}"
    except Exception:
        return dist


def _has_tests(dest: Path) -> bool:
    if any((dest / n).exists() for n in ("tests", "conftest.py", "pytest.ini")):
        return True
    return any(
        p.name.startswith("test_") or p.stem.endswith("_test")
        for p in dest.rglob("*.py")
    )


def _python_third_party_requirements(dest: Path) -> list[str] | None:
    """Requirement lines derived from the tree's real imports, or ``None`` when
    ``dest`` is not a Python tree (no ``.py`` — nothing to scaffold here).

    The third-party top-level modules the code actually imports (stdlib and
    in-tree local modules excluded), mapped to their installed distribution +
    pinned version when resolvable, plus the test runner when a suite is present.
    Deterministic (sorted, no clock) so the integrated ``tree_sha`` is stable
    across the repeated materializations of one run.
    """
    py_files = list(dest.rglob("*.py"))
    if not py_files:
        return None
    # In-tree local names: a top-level dir or a top-level module file. An import
    # of one of these resolves locally and is NOT a third-party dependency.
    local = {p.name for p in dest.iterdir() if p.is_dir()}
    local |= {p.stem for p in dest.glob("*.py")}
    stdlib = sys.stdlib_module_names  # guaranteed on the pinned floor (requires-python >=3.11)
    imported: set[str] = set()
    for path in py_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
    third_party = {n for n in imported if n and n not in stdlib and n not in local}
    try:
        pkg_dist = _im.packages_distributions()
    except Exception:
        pkg_dist = {}
    reqs: set[str] = set()
    for name in third_party:
        dists = pkg_dist.get(name)
        if dists:
            reqs.update(_pin(d) for d in dists)
        else:
            reqs.add(name)  # unresolved import: list the bare name, still auditable
    if _has_tests(dest):
        reqs.add(_pin("pytest"))
    if any((dest / FEATURE_DIR).glob("*.feature")):
        reqs.update((_pin("pytest"), _pin("pytest-bdd")))
    return sorted(reqs)


def _scaffold_manifest(dest: Path) -> None:
    """Ensure a Python ``dest`` ships a recognized dependency manifest.

    No-op when the engineers already shipped one or the tree is not Python.
    Otherwise writes a ``requirements.txt`` derived from the tree's real imports.
    """
    from foundry.gates.audit import recognized_manifests

    if recognized_manifests(dest):
        return
    reqs = _python_third_party_requirements(dest)
    if reqs is None:
        return
    header = (
        SCAFFOLD_HEADER
        + "# Derived from the modules this project imports so the dependency/license\n"
        "# audit can ground its SBOM. Repin or extend as the code evolves.\n"
    )
    body = "".join(f"{line}\n" for line in reqs)
    (dest / "requirements.txt").write_text(header + body, encoding="utf-8")


# ---- gate-owned contract files (the wave-0 contracts ticket) -----------------


def contract_paths(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """The files of the live contracts ticket: gate-owned once it is built."""
    newest: Ticket | None = None
    for a in sorted(_of_kind(state, TICKET_KIND), key=lambda a: a.seq):
        t = _resolve_typed(a, resolve, Ticket, TICKET_KIND)
        if t.id == CONTRACTS_TICKET_ID:
            newest = t
    if newest is None or newest.supersedes == newest.id:
        return ()
    return newest.files


def _wave0_ticket_ids(
    state: FactoryState, resolve: ArtifactResolver
) -> tuple[set[str], set[str]]:
    """(ids of the tickets with no dependency on another ticket, every ticket id)."""
    newest: dict[str, Ticket] = {}
    for a in _of_kind(state, TICKET_KIND):
        t = _resolve_typed(a, resolve, Ticket, TICKET_KIND)
        if t.id not in newest or t.seq > newest[t.id].seq:
            newest[t.id] = t
    known = set(newest)
    wave0 = {t.id for t in newest.values() if not any(d in known for d in t.depends_on)}
    return wave0, known


def _is_fix(task_id: str) -> bool:
    return task_id.startswith("fix-")  # the fixer's FeatureDiffs (make_fixer_node)


def _write_contracts(
    dest: Path, state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> None:
    """Rewrite the contract files as they stood at the end of wave 0 plus every
    later FIXER edit to them, so a later ticket's edit to an agreed signature never
    reaches the gates (the planner also keeps those paths out of every other
    ticket's files) while a repair to the contract itself still can.

    Wave 0 — the tickets with no dependencies, plus the fixes the per-wave check
    ran on them — is every diff integrated before the first later-wave diff. After
    it only fix diffs (``fix-*``, never a planned ticket's id) touch the contracts;
    the fixer records each such change in triage (``note_fix_writes``)."""
    paths = contract_paths(state, resolve)
    if not paths:
        return
    diffs = [(a, _resolve_typed(a, resolve, FeatureDiff, FEATURE_DIFF_KIND))
             for a in _of_kind(state, FEATURE_DIFF_KIND)]
    if not any(d.task_id == CONTRACTS_TICKET_ID for _, d in diffs):
        return  # not built yet: nothing agreed to restore
    wave0, known = _wave0_ticket_ids(state, resolve)
    frozen_at = min(
        (a.seq for a, d in diffs
         if d.task_id not in wave0 and (d.task_id in known or not _is_fix(d.task_id))),
        default=None,
    )
    owned = [a for a, _ in diffs if frozen_at is None or a.seq < frozen_at]
    fixes = [a for a, d in diffs if frozen_at is not None and a.seq >= frozen_at
             and _is_fix(d.task_id) and d.task_id not in known]
    with tempfile.TemporaryDirectory(prefix="foundry_contracts_") as tmp:
        base = state.get("base_dir")
        if base:
            # A change extends the base's contract files: its diffs patch them.
            for rel in paths:
                source = _contained_target(Path(base), rel)
                if source.is_file():
                    target = _contained_target(Path(tmp), rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
        _apply_diffs(Path(tmp), owned, resolve, resolve_blob)
        _apply_diffs(Path(tmp), fixes, resolve, resolve_blob, only=frozenset(paths))
        for rel in paths:
            source = _contained_target(Path(tmp), rel)
            target = _contained_target(dest, rel)
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            elif target.is_file():
                target.unlink()


def _with_base_features(planned: dict[str, str], base: dict[str, str]) -> dict[str, str]:
    """The base build's scenarios stay in force beside the change's. A planned
    ticket reusing a base feature file's name would silently replace those
    scenarios, so it is refused fail-closed (the planner checks for it too)."""
    clash = sorted(set(planned) & set(base))
    if clash:
        raise MaterializationError(
            f"planned feature file(s) {', '.join(clash)} would replace the base "
            "build's scenarios — a change must use new ticket ids"
        )
    return {**base, **planned}


# Files materialize_workdir writes for the gates themselves, not product code.
GATE_INPUT_FILES = frozenset({"criteria.json"})


def materialize_workdir(
    state: FactoryState, dest: Path, *, resolve: ArtifactResolver, resolve_blob: BlobResolver,
    scaffold_manifest: bool = True,
) -> str:
    """Write the integrated tree into ``dest`` and return its ``tree_sha``.

    Builds the workdir the deterministic gates run against from the
    content-addressed artifacts on ``state``:

    * ``criteria.json`` — from the AcceptanceCriterion handles, in the exact
      shape SpecGate parses (``id`` / ``text`` / ``oracle`` / ``security_abuse``),
      resolved via the typed ``resolve`` (the SAME seam senior_engineer / qa use).
    * code + test sources — from the FeatureDiff handles: ``resolve`` gives the
      typed :class:`FeatureDiff`, and its ``diff_uri`` body — a UNIFIED-DIFF blob
      (DS9) — is fetched through the SEPARATE :data:`BlobResolver` seam
      (``sandbox.get_blob``) and applied by the Phase-1 additions-only applier.
      Every target path is containment-checked (D-R1-03) before any write.

    Two DISTINCT seams (D-R1-01/02): ``resolve`` reconstitutes TYPED artifacts
    from their handles; ``resolve_blob`` fetches the raw diff BODY keyed on its
    ``cas://`` uri — so a kind-aware CAS resolver cannot mis-dispatch the diff
    body as a typed artifact.

    Fail-closed (governance §1b): a handle present on state but unresolvable, or
    a diff body of an unsupported hunk shape / escaping path, raises a
    :class:`MaterializationError` — the boundary never runs against a partial
    tree. Returns the integrated ``tree_sha`` (DS4) to use as the verdict's
    ``run_against`` (the digest the FAIL latch keys on).
    """
    dest.mkdir(parents=True, exist_ok=True)
    # A change build starts from the shipped base tree; the diffs are its delta.
    base = state.get("base_dir")
    if base:
        copy_base_tree(base, dest)
    # Spec gate input: the criteria document, byte-stable across folds.
    (dest / "criteria.json").write_text(
        as_text(_criteria_payload(state, resolve)), encoding="utf-8"
    )
    # Executable BDD: written before the sources so an agent patch against a
    # feature applies, then again after so the trusted copy is what the gates see.
    features = _features(state, resolve)
    if base:
        features = _with_base_features(features, base_features(base))
    _write_features(dest, features)
    # Build / Test gate inputs: the integrated code + test sources.
    _write_sources(dest, state, resolve, resolve_blob)
    _write_features(dest, features, prune=True)
    _write_contracts(dest, state, resolve, resolve_blob)
    # Guarantee an auditable dependency manifest so the AUDIT gate can ground its
    # SBOM (no-op when the engineers already shipped one, or the tree is non-Python).
    # An agent's worktree skips it: an engineer handed the scaffold keeps its header
    # and all, and a scaffolded manifest is never installed.
    stack = state.get("stack")
    if scaffold_manifest and (stack is None or stack.language == "python"):
        _scaffold_manifest(dest)
    # The stack's commands are part of what a verdict judges, so they join the
    # toolchain digest: the stable-FAIL latch keys on run_against, and a re-planned
    # stack must be able to re-gate the same source. No stack keeps the old digest.
    return tree_sha(dest, toolchain_digest=_toolchain_digest(stack))


def _toolchain_digest(stack: StackDecision | None) -> str:
    if stack is None:
        return _TOOLCHAIN_DIGEST
    return f"{_TOOLCHAIN_DIGEST}+stack:" + content_digest(stack.model_dump(mode="json"))


# ---- gate-run helpers (verdicts ONLY from the runner) -----------------------

# Spec is keyed under the criteria/diff emission point; build/test under their
# own keys so each verdict gets an independent resume-monotonic epoch stream.
_SPEC_NODE = "spec_gate"
_BUILD_NODE = "build_gate"
_TEST_NODE = "test_gate"
_SMOKE_NODE = "smoke_gate"


def _run_gate(
    gate: GateImpl,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    config: RunnableConfig,
    run_against: str,
    workdir: Path,
    node_id: str,
) -> GateResult:
    """Run ONE gate via the runner and return its ATTESTED verdict.

    Mints the verdict's ``Seq`` via ``rt.seq`` (resume-monotonic epoch, AR2)
    keyed on the gate's grounding so a re-execution outranks the stale record it
    supersedes. The runner attests the result; we never sign or construct one.
    """
    seq = rt.seq(config, node_id, f"verdict:{gate.name}")
    return runner.run(gate, run_against, seq, workdir)


def gate_profile(
    state: FactoryState, fallback: VerificationProfile | None
) -> VerificationProfile | None:
    """The gate commands for this run, read at gate time from the ``stack`` channel.

    A ``--profile`` pin runs that profile unchanged; a planner stack in a language
    this host has runs its own commands; a stack in an undetected language is an
    ``unavailable`` profile (every gate an infra ERROR naming the missing toolchain —
    never a silent switch to another stack's commands); no stack keeps ``fallback``
    (the profile the graph was built with)."""
    from foundry.gates.profiles import PROFILES, VerificationProfile, stack_profile
    from foundry.toolchains import available_languages

    stack = state.get("stack")
    if stack is None:
        return fallback
    if stack.profile:
        return PROFILES.get(stack.profile, fallback)
    available = available_languages()
    if stack.language not in available:
        return VerificationProfile(
            name=f"stack:{stack.language}",
            unavailable=(
                f"missing toolchain: the planned stack's language {stack.language!r} "
                f"is not installed on the gate host (detected: "
                f"{', '.join(sorted(available)) or 'none'})"
            ),
        )
    return stack_profile(stack)


class UnavailableGate:
    """A gate that cannot run on this host: zone A raises ``GroundingUnavailable`` so
    the runner signs an infra ERROR (bounded retry, never latched) naming ``reason``."""

    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True
    mutant_rotation_id = 0
    scanner_rule_version = "unavailable-v1"

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def execute(self, workdir: Path) -> ZoneAObservation:
        from foundry.gates.runner import GroundingUnavailable

        raise GroundingUnavailable(self._reason)

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.ERROR


class UnpinnedSmokeGate:
    """The smoke verdict for a stack with no run command: a deterministic PASS that
    says so. Nothing executes; the verdict still comes from the runner, attested,
    so the boundary's smoke slot is never filled by an unsigned record."""

    name = "smoke"
    verdict_class = VerdictClass.CONFIG_EVALUABLE
    decision_deterministic = True
    execution_deterministic = False

    def __init__(self, *, seal_key: bytes | None = None) -> None:
        self._seal_key = seal_key

    def execute(self, workdir: Path) -> ZoneAObservation:
        from foundry.gates.runner import observe

        return observe(
            self.name,
            structured={"pinned": False},
            findings=("smoke not pinned: the stack declares no run command",),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS if obs.structured.get("pinned") is False else GateStatus.ERROR


def _build_gate(
    profile: VerificationProfile | None,
    runner: TwoZoneGateRunner,
    pyenv: PythonBuildEnv | None = None,
) -> GateImpl:
    if profile is None:
        return BuildGate(seal_key=runner.seal_key, pyenv=pyenv)
    if profile.unavailable:
        return UnavailableGate("build", profile.unavailable)
    return BuildGate(
        cmd=profile.build_cmd,
        install_cmd=profile.install_cmd,
        seal_key=runner.seal_key,
        profile_name=profile.name,
        pyenv=pyenv,
    )


@contextlib.contextmanager
def _build_env(
    profile: VerificationProfile | None, workdir: Path, rt: AgentRuntime
) -> Iterator[PythonBuildEnv | None]:
    """The build's venv when it is a Python build that declares dependencies: the
    run's (``rt.python_envs``) for the same manifests, so checks reuse one install.
    Every gate step inside shares the run's toolchain cache (``rt.toolchain_cache``):
    what the install step downloads, the offline steps can read."""
    python = profile is None or (profile.python and not profile.unavailable)
    with toolchain_scope(rt.toolchain_cache), python_build_env(
        workdir, python=python, cache=rt.python_envs
    ) as pyenv:
        yield pyenv


def run_spec_gate(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
) -> dict[str, Any]:
    """Run the Spec gate at a product / architecture exit boundary.

    Materializes the workdir (criteria.json + any sources present) into
    ``workdir`` and runs :class:`SpecGate` through the runner against the
    integrated ``tree_sha``. Returns ``{"gates": [<attested GateResult>]}`` —
    the ONLY thing that reaches the channel, and only because the runner
    attested it (an unsigned write would be dropped, L5).
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    result = _run_gate(
        SpecGate(seal_key=runner.seal_key),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_SPEC_NODE,
    )
    return {"gates": [result]}


def run_build_test_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    min_passed: int = 1,
    discriminator_factory: DiscriminatorFactory | None = None,
    profile: VerificationProfile | None = None,
) -> dict[str, Any]:
    """Run the Build + Test + Smoke gates at an implementation /
    quality exit boundary.

    Materializes the integrated tree (code + test sources + criteria.json) into
    ``workdir`` ONCE and runs :class:`BuildGate` then :class:`TestGate` against
    its ``tree_sha`` (the same integrated digest, so both verdicts latch/bind to
    one state). ``min_passed`` is the Test gate's adequacy floor — a vacuous
    (0-test) suite is FAIL, never PASS (governance §1c). Returns
    ``{"gates": [<attested build>, <attested test>, <attested smoke>]}``; the
    commands come from :func:`gate_profile` (the stack on state, else ``profile``).

    ``discriminator_factory`` is the OPTIONAL malicious-test defense seam (the
    A-M4-01 fix). When provided, it produces a
    :class:`~foundry.gates.discriminator.TestSuiteDiscriminator` (the curated
    held-out mutants + behavioral invariants for the subject under test) that the
    constructed :class:`TestGate` CONSULTS in zone A: a gamed-green suite that
    clears the adequacy floor but does NOT kill a held-out mutant (or violates an
    invariant) is FAILed by the COMPILED gate — not merely in the unit test (gov
    §1c). The held-out mutant SET is a dev-grade CURATED set the factory supplies
    (a real mutation engine is heavier; plan §0.3). When ``None`` (the default),
    behavior is EXACTLY as before — the adequacy floor alone (back-compat): no
    discriminator is wired and a green + adequate suite suffices.
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    # Profile-pinned commands (ROADMAP Phase 3): a stack-appropriate profile
    # swaps WHAT the gates execute, never the two-zone trust seam. None keeps
    # each gate's historical default — byte-identical to the pre-profile factory.
    profile = gate_profile(state, profile)
    with _build_env(profile, workdir, rt) as pyenv:
        return _build_test_smoke(
            state, config, rt=rt, runner=runner, workdir=workdir, run_against=run_against,
            min_passed=min_passed, discriminator_factory=discriminator_factory,
            profile=profile, pyenv=pyenv,
        )


def _build_test_smoke(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    workdir: Path,
    run_against: str,
    min_passed: int,
    discriminator_factory: DiscriminatorFactory | None,
    profile: VerificationProfile | None,
    pyenv: PythonBuildEnv | None,
) -> dict[str, Any]:
    test_cmd = profile.test_cmd if profile is not None else None
    build = _run_gate(
        _build_gate(profile, runner, pyenv),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_BUILD_NODE,
    )
    # Wire the held-out-mutant + behavioral-invariance discriminator into the
    # COMPILED Test gate when a factory is injected (A-M4-01): a gamed-green suite
    # that clears the floor but survives a held-out mutant FAILs here, not just in
    # the unit test. Absent a factory the gate is the legacy adequacy-floor-only
    # Test gate (back-compat).
    discriminator = discriminator_factory() if discriminator_factory is not None else None
    # A profile-pinned FOREIGN test harness (npm test / terraform validate) has no
    # pytest summary to count, so the adequacy floor drops to exit-code-only there
    # (min_passed=0; the floor is pytest-specific). The python default keeps it.
    effective_min = 0 if test_cmd is not None else min_passed
    test_gate: GateImpl
    if profile is not None and (profile.unavailable or profile.test_unavailable):
        test_gate = UnavailableGate("test", profile.unavailable or profile.test_unavailable)
    else:
        test_gate = TestGate(
            cmd=test_cmd,
            min_passed=effective_min,
            discriminator=discriminator,
            seal_key=runner.seal_key,
            bdd_cmd=profile.bdd_cmd if profile is not None else None,
            # Profile commands (npm test, go test, ...) are judged by exit code, not
            # pytest's codes; None keeps the pinned pytest harness and its semantics.
            stack_harness=test_cmd is not None,
            profile_name=profile.name if profile is not None else "",
            pyenv=pyenv,
        )
    test = _run_gate(
        test_gate,
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_TEST_NODE,
    )
    gates = [build, test]
    out: dict[str, Any] = {"gates": gates}
    if profile is not None and test_cmd is not None and profile.bdd_cmd is None and any(
        (workdir / FEATURE_DIR).glob("*.feature")
    ):
        # Only the pytest harness executes scenarios; say so rather than pass silently.
        out["gate_triage"] = [GateTriage(
            name=test.name,
            run_against=run_against,
            explanation=(
                "ADVISORY: features/*.feature hold the tickets' acceptance scenarios, "
                f"but the {profile.name} test harness does not execute them — they "
                "are not enforced as executable for this stack."
            ),
            produced_by=_TEST_NODE,
            seq=rt.seq(config, _TEST_NODE, "triage:bdd"),
        )]
    # Every run carries a smoke verdict: the stack's own run command, else an
    # attested "not pinned" PASS (the boundary is always build/test/smoke).
    from foundry.gates.smoke import SmokeGate

    smoke: GateImpl
    if profile is not None and profile.unavailable:
        smoke = UnavailableGate("smoke", profile.unavailable)
    elif profile is not None and profile.smoke_cmd is not None:
        smoke = SmokeGate(profile.smoke_cmd, seal_key=runner.seal_key, pyenv=pyenv)
    else:
        smoke = UnpinnedSmokeGate(seal_key=runner.seal_key)
    gates.append(
        _run_gate(
            smoke,
            rt=rt,
            runner=runner,
            config=config,
            run_against=run_against,
            workdir=workdir,
            node_id=_SMOKE_NODE,
        )
    )
    return out


def wave_scenarios(state: FactoryState, resolve: ArtifactResolver) -> frozenset[str]:
    """The scenario refs a per-wave check can judge: those of criteria whose every
    carrier (the owning ticket and each ticket listing it in ``scenario_ids``, i.e.
    its paired test ticket) is built. Anything still waiting on a ticket is left to
    a later wave or the final gate."""
    from foundry.agents.implementation.tickets import built_task_ids, read_tickets

    built = built_task_ids(state)
    carriers: dict[str, set[str]] = {}
    for t in read_tickets(state, resolve):
        for cid in t.scenario_ids:
            carriers.setdefault(cid, set()).add(t.id)
    return frozenset(
        scenario_ref(c.ticket_id, c.id)
        for c in _current_criteria(state, resolve).values()
        if c.ticket_id and {c.ticket_id, *carriers.get(c.id, ())} <= built
    )


def _wave_test_gate(
    profile: VerificationProfile | None,
    runner: TwoZoneGateRunner,
    scope: frozenset[str],
    pyenv: PythonBuildEnv | None = None,
) -> TestGate | None:
    """The scoped Test gate for a wave check, or ``None`` when this stack's harness
    does not execute scenarios (a foreign test command with no ``bdd``)."""
    if profile is not None and (
        profile.unavailable or profile.test_unavailable
        or (profile.test_cmd is not None and profile.bdd_cmd is None)
    ):
        return None
    return TestGate(
        cmd=profile.test_cmd if profile is not None else None,
        seal_key=runner.seal_key,
        bdd_cmd=profile.bdd_cmd if profile is not None else None,
        stack_harness=profile is not None and profile.test_cmd is not None,
        profile_name=profile.name if profile is not None else "",
        scope=scope,
        pyenv=pyenv,
    )


def run_build_gate(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    profile: VerificationProfile | None = None,
    scenarios: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Run the Build gate on the integrated tree (the per-wave check, §2b), plus,
    when ``scenarios`` is non-empty, the Test gate scoped to those scenarios, so an
    unbound or failing scenario is caught in the wave that built it.

    The full Test gate is left out on purpose: scenarios of tickets not yet built
    would fail it. Keyed under the same nodes as the final gates, so the final
    verdicts (a later epoch) supersede every intermediate one."""
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    out: dict[str, Any] = {}
    stack = state.get("stack")
    if stack is not None and not stack.profile:
        files = [f for a in _of_kind(state, TICKET_KIND)
                 for f in _resolve_typed(a, resolve, Ticket, TICKET_KIND).files]
        notes: list[tuple[str, str]] = []
        # Python installs only what a built manifest declares; its build needs none.
        missing = () if stack.language == "python" else unbuilt_manifests(files, workdir)
        if missing:
            # No manifest, nothing to install from; the build and the type checker
            # need the dependencies the install would have fetched.
            skip = ("install", "build", "typecheck")
            dropped = [f"{k} `{getattr(stack, k)}`" for k in skip if getattr(stack, k)]
            if dropped:
                stack = stack.model_copy(update=dict.fromkeys(skip, ""))
                notes.append((WAVE_MANIFEST_TRIAGE, (
                    f"this wave's check skips {', '.join(dropped)}: the planned "
                    f"manifest {', '.join(missing)} is not built yet; the final gate "
                    "runs every step")))
        for step in ("build", "typecheck"):
            full = getattr(stack, step)
            if not full:
                continue
            scoped = wave_typecheck(full, planned_paths(files), workdir)
            if scoped != full:
                stack = stack.model_copy(update={step: scoped})
                notes.append((WAVE_TYPECHECK_TRIAGE, (
                    f"this wave's {step} skips planned targets no ticket has built "
                    f"yet: `{scoped or '(skipped: none built yet)'}` instead of `{full}`")))
        if notes:
            # The scoped commands are what this verdict judges, so they key the digest.
            state = {**state, "stack": stack}
            run_against = tree_sha(workdir, toolchain_digest=_toolchain_digest(stack))
            out["gate_triage"] = [GateTriage(
                name=name, run_against=run_against, explanation=text[:2000],
                produced_by=_BUILD_NODE,
                seq=rt.seq(config, _BUILD_NODE, f"{name}:{run_against}"),
            ) for name, text in notes]
    profile = gate_profile(state, profile)
    with _build_env(profile, workdir, rt) as pyenv:
        build = _run_gate(
            _build_gate(profile, runner, pyenv),
            rt=rt,
            runner=runner,
            config=config,
            run_against=run_against,
            workdir=workdir,
            node_id=_BUILD_NODE,
        )
        out["gates"] = [build]
        test = _wave_test_gate(profile, runner, scenarios, pyenv) if scenarios else None
        if test is not None:
            # Same tree digest as the build, so the fixer reads both verdicts as this
            # pass's. A scoped FAIL latching the tree is safe: the final Test gate only
            # ever runs on a later tree (a failing wave check is fixed or halts).
            out["gates"].append(_run_gate(
                test, rt=rt, runner=runner, config=config, run_against=run_against,
                workdir=workdir, node_id=_TEST_NODE,
            ))
    return out


WAVE_TYPECHECK_TRIAGE = "wave-typecheck-scope"
WAVE_MANIFEST_TRIAGE = "wave-manifest-scope"
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
# The package manifests a non-Python stack's install, build and typecheck need.
_MANIFESTS = frozenset({"package.json", "pyproject.toml", "Cargo.toml", "go.mod"})


def unbuilt_manifests(files: Iterable[str], workdir: Path) -> tuple[str, ...]:
    """The planned package manifests no ticket has written into ``workdir`` yet."""
    return tuple(sorted({
        p for p in (_norm_target(f) for f in files)
        if PurePosixPath(p).name in _MANIFESTS and not (workdir / p).exists()
    }))


def planned_paths(files: Iterable[str]) -> frozenset[str]:
    """Every path the plan will create: each ticket file and its parent dirs
    (``app/db.py`` plans ``app`` too), normalized with no ``./`` or trailing ``/``."""
    out: set[str] = set()
    for f in files:
        p = PurePosixPath(_norm_target(f))
        if str(p) in ("", "."):
            continue
        out.add(str(p))
        out.update(str(d) for d in p.parents if str(d) != ".")
    return frozenset(out)


def _norm_target(token: str) -> str:
    return token.removeprefix("./").rstrip("/")


def wave_typecheck(typecheck: str, planned: frozenset[str], workdir: Path) -> str:
    """The stack's typecheck for an intermediate wave: arguments naming a planned
    path that no ticket has built yet are dropped (``mypy app`` before the wave
    that writes ``app/`` would fail on the missing target, not on the code). If
    that leaves none of the command's planned targets, the step is skipped (``""``).
    Anything else — a target outside the plan, a path already built — is kept, so
    a real type error or a bad target still fails the check; the final Build gate
    always runs the full command."""
    try:
        tokens = shlex.split(typecheck)
    except ValueError:
        return typecheck

    def targeted(tok: str) -> bool:
        return _norm_target(tok) in planned

    def unbuilt(tok: str) -> bool:
        return targeted(tok) and not (workdir / _norm_target(tok)).exists()

    if not any(unbuilt(t) for t in tokens):
        return typecheck
    kept = [t for t in tokens if not unbuilt(t)]
    if not any(targeted(t) for t in kept):
        return ""
    return " ".join(t if t in _SHELL_OPERATORS else shlex.quote(t) for t in kept)


# ---- routing predicate (fail-closed, convergence-bounded) -------------------

# Which gate verdicts each phase boundary is responsible for.
SPEC_BOUNDARY = "spec"
BUILD_TEST_BOUNDARY = "build_test"
# The per-wave integration check (§2b): the build verdict alone.
BUILD_BOUNDARY = "build"
# The final boundary: build, test and smoke (a stack without a run command gets
# an attested "not pinned" smoke PASS), so a missing/FAILed smoke never advances.
BUILD_TEST_SMOKE_BOUNDARY = "build_test_smoke"

_BOUNDARY_GATES: dict[str, frozenset[str]] = {
    SPEC_BOUNDARY: frozenset({"spec"}),
    BUILD_BOUNDARY: frozenset({"build"}),
    BUILD_TEST_BOUNDARY: frozenset({"build", "test"}),
    BUILD_TEST_SMOKE_BOUNDARY: frozenset({"build", "test", "smoke"}),
}

# Routing outcomes.
ADVANCE = "advance"  # all relevant gates PASS — proceed to the next phase
FIX = "fix"  # a FAIL/ERROR within the convergence bound — back to the layer
HALT = "halt"  # bound exhausted or a stable-FAIL latch — escalate / stop
# Hybrid only: the wave check failed twice; re-run its local tickets once on the
# cloud level (WaveBuildCheck.route; never returned by route_after_gates).
ESCALATE = "escalate"


def _relevant_latest(
    state: FactoryState, gate_names: frozenset[str]
) -> dict[str, GateResult]:
    """Newest-by-Seq verdict per relevant gate name on ``state['gates']``.

    The channel is already trust-filtered (``merge_gates`` admitted only
    runner-attested records) and keyed by ``(name, run_against)``; pick the
    latest by ``Seq`` per name so a re-run's verdict (bumped epoch) wins.
    """
    latest: dict[str, GateResult] = {}
    for g in state.get("gates") or ():
        if g.name not in gate_names:
            continue
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def _is_flake_quarantine(g: GateResult) -> bool:
    """An execution gate's ERROR caused by flake quarantine, not a hard verdict.

    The N=2 flake rule (E2E) and a quarantining Test run surface a split/flaky
    outcome as ERROR (DS7 — a flake is a property of the execution, not the
    tree). Such an ERROR must NOT fail the boundary: it quarantines (routes to
    bounded retry), never latches, never advances. Detected by gate family +
    a quarantine/flake marker in the runner findings.
    """
    if g.status is not GateStatus.ERROR or g.name not in {"test", "e2e"}:
        return False
    blob = " ".join(g.findings).lower()
    return "flak" in blob or "quarantin" in blob


def flake_quarantined(state: FactoryState, *, boundary: str) -> bool:
    """True iff a relevant Test/E2E verdict at ``boundary`` is flake-quarantined.

    A supervisor seam: distinguishes a transient flake-quarantine ERROR (retry
    without disqualifying the digest — it never latches and must NOT fail the
    boundary, governance §1a) from a hard FAIL / infra ERROR. The routing
    predicate already keeps a quarantine ERROR out of ADVANCE; this exposes the
    REASON so the supervisor can quarantine-and-retry rather than report a
    failure.
    """
    gate_names = _BOUNDARY_GATES.get(boundary, frozenset())
    return any(
        _is_flake_quarantine(g) for g in _relevant_latest(state, gate_names).values()
    )


def _fix_attempts(state: FactoryState, config: RunnableConfig) -> int:
    """The convergence bound's spent attempts (architecture.md §7).

    Read from the budget ledger's global ``fix_attempts_since_all_green`` if
    present, else from the run config — the supervisor owns the counter; the
    routing predicate only READS it (fold-order independent)."""
    budget = state.get("budget")
    if budget is not None:
        return int(budget.fix_attempts_since_all_green)
    configurable = config.get("configurable") or {}
    return int(configurable.get("fix_attempts", 0))


def _fix_total(state: FactoryState) -> int:
    budget = state.get("budget")
    return int(budget.fix_attempts_total) if budget is not None else 0


def fix_cap(state: FactoryState, resolve: ArtifactResolver, max_fix_attempts: int) -> int:
    """The run's overall fix bound: ``max_fix_attempts`` per wave check plus the
    final gate's, so per-boundary resets can never add up to an unbounded loop."""
    from foundry.agents.implementation.tickets import plan_wave_count, read_tickets

    return max_fix_attempts * (plan_wave_count(read_tickets(state, resolve)) + 1)


def route_after_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int = 3,
    total_cap: int | None = None,
) -> str:
    """Route a phase boundary on its relevant gate verdicts. Fail-closed.

    Returns:

    * :data:`ADVANCE` — every relevant gate has a PASS verdict on a non-disqualified
      digest; proceed to the next phase.
    * :data:`FIX` — a FAIL / (non-flake) ERROR, a missing relevant verdict, OR a
      stable-FAIL-latched digest, WITHIN the convergence bound; route back to the
      responsible layer to fix FORWARD into a new tree.
    * :data:`HALT` — the ``max_fix_attempts`` convergence bound is exhausted (escalate
      with the best draft); a non-fixable terminal condition. ``total_cap`` bounds the
      build-side fixes of the whole run (``budget.fix_attempts_total``), which the
      per-boundary resets never clear.

    The stable-FAIL latch (committed cross-fork; ``records``) permanently DISQUALIFIES
    the failing digest — a later PASS on that SAME tree never clears it (governance §5,
    anti-gaming) — but it no longer ends the run on its own: the bounded fix loop lets
    the engineers produce a NEW, passing tree (a real-shop fix-forward), and HALT fires
    only when the bound is spent. A flake-quarantine ERROR (N=2 split) is likewise a
    bounded retry, never a boundary FAIL/advance on its own.
    """
    gate_names = _BOUNDARY_GATES.get(boundary)
    if gate_names is None:
        raise ValueError(f"unknown phase boundary: {boundary!r}")

    latest = _relevant_latest(state, gate_names)

    bound_spent = _fix_attempts(state, config) >= max_fix_attempts or (
        total_cap is not None and _fix_total(state) >= total_cap
    )

    # A relevant gate needs a fix when it FAILs, ERRORs, is MISSING (never advance on
    # an absent verdict), OR its digest carries a stable-FAIL latch. A latched digest
    # is permanently DISQUALIFIED — it can never be admitted, and a later (flaky/gamed)
    # PASS on that SAME tree never clears it (governance §5, anti-gaming preserved). But
    # a disqualified tree does NOT end the run: like a real shop whose CI failed, the
    # engineers fix FORWARD into a NEW tree (a new digest the latch never touched). We
    # therefore route to the BOUNDED fix loop and HALT only once ``max_fix_attempts`` is
    # spent — escalate then, rather than shipping nothing on the first failure.
    needs_fix = False
    for name in gate_names:
        verdict = latest.get(name)
        if verdict is None:
            needs_fix = True  # missing relevant verdict: never advance (fail-closed)
            continue
        if verdict.status in (GateStatus.FAIL, GateStatus.ERROR):
            # FAIL, infra ERROR, OR flake-quarantine ERROR all route to bounded
            # retry — none advances the boundary (fail-closed).
            needs_fix = True
        elif records.stable_fail_latched(verdict.run_against, verdict.name):
            # A disqualified digest showing a PASS must NOT advance — route to a fresh
            # attempt instead (the latch can never be cleared on this tree).
            needs_fix = True

    if needs_fix:
        return HALT if bound_spent else FIX

    # All relevant gates PASS on a non-disqualified digest.
    return ADVANCE


_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)")
_BDD_SCENARIO = re.compile(r"^BDD criterion \S+: scenario (\S+)")


def failing_ids(findings: Iterable[str]) -> tuple[str, ...]:
    """The failing pytest node ids and BDD scenario refs a gate's findings name."""
    out: list[str] = []
    for line in findings:
        m = _BDD_SCENARIO.match(line) or _FAILED_TEST.match(line.strip())
        if m is not None and m.group(1) not in out:
            out.append(m.group(1))
    return tuple(out)


# foundry/gates/build.py's head for a pip install that failed on the project's own
# manifest (not the network): the fix is a manifest edit, never a re-run.
_PIP_MANIFEST_FAIL = "declared dependencies cannot be installed"
PIP_MANIFESTS = ("requirements.txt", "pyproject.toml")
# A relative source path with a line number: mypy/pyright/tsc/go/rustc/javac errors.
_SOURCE_LINE = re.compile(
    r"(?<![\w./-])(?:\./)?((?:[\w-][\w.-]*/)*[\w-][\w.-]*\."
    r"(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|cs|c|cc|cpp|h|hpp|rb))[:(]\d+"
)
# A Python compile error / traceback frame: File "./app.py", line 3.
_PY_FRAME = re.compile(r'File "(?:\./)?([^"/][^"]*\.pyi?)", line \d+')
_VENDORED = ("node_modules", "site-packages", ".venv", "venv", "vendor")
# pytest-bdd's errors for a step its test module cannot run: a step argument nothing
# provides, a step line no definition matches.
_STEP_BINDING_ERROR = re.compile(r"fixture '\w+' not found|StepDefinitionNotFoundError")


def pip_manifest_failure(gates: Iterable[GateResult]) -> bool:
    """True iff a FAILed gate says pip could not install the project's declared
    dependencies (a bad pin or a package that does not exist)."""
    return any(g.status is GateStatus.FAIL and any(_PIP_MANIFEST_FAIL in f for f in g.findings)
               for g in gates)


def failure_files(gates: Iterable[GateResult]) -> tuple[str, ...]:
    """The files a failure names, in order: the pip manifests for a manifest FAIL,
    then the sources a build gate's compile/type errors point at, then the test
    modules of a test gate's step-binding errors. Other test-gate tracebacks are
    left out: they name the test file, rarely where the fix goes; a step a test
    module cannot bind is fixed in that module."""
    gates = list(gates)
    out: list[str] = list(PIP_MANIFESTS) if pip_manifest_failure(gates) else []
    for g in gates:
        if g.status not in (GateStatus.FAIL, GateStatus.ERROR):
            continue
        if g.name == "test":
            if any(_STEP_BINDING_ERROR.search(f) for f in g.findings):
                out.extend(dict.fromkeys(
                    p for p in (i.split("::", 1)[0] for i in failing_ids(g.findings))
                    if p.endswith(".py") and p not in out))
            continue
        if g.name != "build":
            continue
        for line in g.findings:
            for m in (*_SOURCE_LINE.finditer(line), *_PY_FRAME.finditer(line)):
                path = m.group(1)
                parts = path.split("/")
                if ".." in parts or any(p in _VENDORED for p in parts) or path in out:
                    continue
                out.append(path)
    return tuple(out)


# Run-varying bits of a raw finding line (object addresses, timings): two runs of
# the same failure must compare equal.
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\b\d+(?:\.\d+)?m?s\b")


def _failure_signature(verdicts: Iterable[GateResult]) -> frozenset[tuple[str, str]] | None:
    """What one tree fails on: (gate, failing test/scenario id) per FAILed gate, or
    (gate, normalized finding line) for a gate whose findings name no id. ``None``
    when a verdict ERRORed: an infra error or a flake quarantine is never "no
    progress"."""
    sig: set[tuple[str, str]] = set()
    for g in verdicts:
        if g.status is GateStatus.ERROR:
            return None
        if g.status is GateStatus.FAIL:
            ids = failing_ids(g.findings) or tuple(_VOLATILE.sub("#", f) for f in g.findings)
            sig.update((g.name, i) for i in ids)
    return frozenset(sig)


def _unchanged_tree_recheck(
    state: FactoryState, out: dict[str, Any], config: RunnableConfig, *, boundary: str
) -> bool:
    """True iff ``out`` re-checks a tree ``state`` already failed on, after a fix pass
    in between: the fixer changed nothing, so the same checks fail by definition.
    (:func:`no_fix_progress` cannot see this: the re-check replaces the earlier
    verdicts under the same ``(name, run_against)`` key.) ERRORs never count."""
    if _fix_attempts(state, config) < 1:
        return False
    names = _BOUNDARY_GATES[boundary]
    new = [g for g in out.get("gates") or () if g.name in names]
    if not new:
        return False
    tree = new[0].run_against
    old = [g for g in state.get("gates") or () if g.name in names and g.run_against == tree]
    prev, cur = _failure_signature(old), _failure_signature(new)
    return bool(old) and bool(prev) and bool(cur)


def failing_streak(state: FactoryState, *, boundary: str) -> int:
    """How many of the newest trees in a row the code itself fails at ``boundary``:
    a FAIL in the newest verdict per gate on the tree. ERROR (an infra error or a
    flake quarantine, see :func:`_failure_signature`) is not a code failure and,
    like a pass, ends the streak."""
    names = _BOUNDARY_GATES[boundary]
    trees: dict[str, dict[str, GateResult]] = {}
    for g in state.get("gates") or ():
        if g.name not in names:
            continue
        newest = trees.setdefault(g.run_against, {})
        if g.name not in newest or g.seq > newest[g.name].seq:
            newest[g.name] = g
    streak = 0
    for verdicts in sorted(trees.values(), key=lambda v: max(g.seq for g in v.values()),
                           reverse=True):
        statuses = {g.status for g in verdicts.values()}
        if GateStatus.FAIL not in statuses or GateStatus.ERROR in statuses:
            break
        streak += 1
    return streak


def no_fix_progress(state: FactoryState, config: RunnableConfig, *, boundary: str) -> bool:
    """True iff the newest tree fails ``boundary`` on exactly what the tree before it
    failed on, after a fix pass in between: the fixer made no progress.

    Only once this boundary's budget window has spent a fix (two failing checks in
    it), so a resumed run's fresh budget re-check never counts as a pass."""
    if _fix_attempts(state, config) < 2:
        return False
    names = _BOUNDARY_GATES[boundary]
    trees: dict[str, dict[str, GateResult]] = {}
    for g in sorted((g for g in state.get("gates") or () if g.name in names),
                    key=lambda g: g.seq):
        trees.setdefault(g.run_against, {})[g.name] = g
    order = sorted(trees.values(), key=lambda v: max(g.seq for g in v.values()))
    if len(order) < 2:
        return False
    prev, cur = (_failure_signature(v.values()) for v in order[-2:])
    return bool(cur) and cur == prev


def bump_fix_attempts(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int,
    total_cap: int | None = None,
) -> None:
    """Bump ``budget.fix_attempts_since_all_green`` on ``out`` when ``boundary``
    will route ``fix`` after ``out`` folds in — and, outside the Spec boundary, the
    run-wide ``fix_attempts_total`` that ``total_cap`` bounds.

    The gate node writing ``out`` is the sole ``budget`` writer of its superstep, and
    the conditional edge that fires in that same superstep reads the post-fold state.
    So the decision folds the prior ``gates`` with the node's delta through the REAL
    reducer — never a hand-assembled subset — so it can never diverge from the edge's
    read (E-R1-01)."""
    ledger = state.get("budget") or BudgetLedger()
    post: FactoryState = {
        "gates": merge_gates(state.get("gates"), out.get("gates")),
        "budget": ledger,
    }
    decision = route_after_gates(
        post, config, boundary=boundary, records=records, max_fix_attempts=max_fix_attempts,
        total_cap=total_cap,
    )
    if decision == FIX:
        out["budget"] = ledger.model_copy(update={
            "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + 1,
            "fix_attempts_total": ledger.fix_attempts_total + (boundary != SPEC_BOUNDARY),
        })


def settle_fix_attempts(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int,
    total_cap: int | None = None,
) -> None:
    """:func:`bump_fix_attempts`, plus a reset to 0 when ``boundary`` ADVANCEs: a
    green boundary hands the next one the whole budget, so the attempts one
    boundary spent (a Spec re-plan, an earlier wave's repair) never leave a later
    failure with fewer fixes. A spent bound (HALT) keeps the ledger unchanged."""
    bump_fix_attempts(state, config, out, boundary=boundary, records=records,
                      max_fix_attempts=max_fix_attempts, total_cap=total_cap)
    ledger = state.get("budget") or BudgetLedger()
    if "budget" in out or not ledger.fix_attempts_since_all_green:
        return
    post: FactoryState = {
        "gates": merge_gates(state.get("gates"), out.get("gates")), "budget": ledger,
    }
    if route_after_gates(post, config, boundary=boundary, records=records,
                         max_fix_attempts=max_fix_attempts, total_cap=total_cap) == ADVANCE:
        out["budget"] = ledger.model_copy(update={"fix_attempts_since_all_green": 0})


@dataclass(frozen=True)
class WaveBuildCheck:
    """The per-wave integration check (plan §2b) the implementation subgraph runs
    between ticket waves: the attested Build gate on the integrated tree, through
    the SAME runner as the supervisor's gates. Each wave has its own fix budget: a
    passing check is a green state for that boundary and resets the counter, so wave
    repairs never eat the next wave's or the final build/test gate's budget. A wave
    gets ``max_fix_attempts - 1`` fixer passes (the default 3 gives 2), and every
    build-side fix also counts toward :func:`fix_cap`, the run-wide bound.

    In hybrid mode a check that fails twice in a row asks for the newest wave's
    local tickets (those the failures name, else all of them) to re-run once on
    the cloud level (``escalate-request`` triage; :data:`ESCALATE` routes back to
    the engineering node), before any further fixer pass or a halt. An
    escalation spends none of the wave's fix attempts. A ticket whose
    kept local session stopped early (turn cap, repeat/read-thrash) is asked for on
    the FIRST failure that names it (or, alone in its wave, on any first failure).

    With ``check_scenarios`` the check also runs the Test gate scoped to the
    scenarios whose tickets are all built (:func:`wave_scenarios`), so an unbound or
    failing scenario reaches the wave's fixer instead of piling up for the final gate.

    ``run`` is the node (verdict + budget bump); ``route`` returns
    :data:`ADVANCE` / :data:`FIX` / :data:`HALT` for the :data:`BUILD_BOUNDARY`, or
    :data:`BUILD_TEST_BOUNDARY` when the check judged scenarios."""

    rt: AgentRuntime
    runner: TwoZoneGateRunner
    resolve: ArtifactResolver
    resolve_blob: BlobResolver
    max_fix_attempts: int = 3
    profile: VerificationProfile | None = None
    check_scenarios: bool = True
    # The engineering node's difficulty -> tier table (escalation picks local tickets).
    ticket_tiers: Mapping[Any, str] | None = None

    def _scenarios(self, state: FactoryState) -> frozenset[str]:
        if not self.check_scenarios:
            return frozenset()
        scope = wave_scenarios(state, self.resolve)
        if not scope or _wave_test_gate(gate_profile(state, self.profile), self.runner,
                                        scope) is None:
            return frozenset()
        return scope

    def _boundary(self, state: FactoryState) -> str:
        return BUILD_TEST_BOUNDARY if self._scenarios(state) else BUILD_BOUNDARY

    def run(self, state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="foundry_wave_gate_") as tmp:
            out = run_build_gate(
                state, config, rt=self.rt, runner=self.runner, resolve=self.resolve,
                resolve_blob=self.resolve_blob, workdir=Path(tmp), profile=self.profile,
                scenarios=self._scenarios(state),
            )
        boundary = self._boundary(state)
        settle_fix_attempts(state, config, out, boundary=boundary,
                            records=self.runner.records, max_fix_attempts=self.max_fix_attempts,
                            total_cap=fix_cap(state, self.resolve, self.max_fix_attempts))
        post: FactoryState = {
            "gates": merge_gates(state.get("gates"), out.get("gates")),
            "budget": out.get("budget") or state.get("budget") or BudgetLedger(),
        }
        decision = self._route(post, config, boundary)
        streak = failing_streak(post, boundary=boundary) if decision != ADVANCE else 0
        picked: tuple[str, ...] = ()
        if streak >= 1:
            from foundry.agents.implementation.tickets import escalation_pick, stopped_tickets
            from foundry.execution.adapters.tiered import (
                ESCALATE_FALLBACK_TRIAGE,
                ESCALATE_REQUEST_TRIAGE,
            )

            failed = ", ".join(sorted({g.name for g in out.get("gates") or ()
                                       if g.status in (GateStatus.FAIL, GateStatus.ERROR)}))
            # A ticket whose local session stopped early escalates on the first
            # failure; any other waits for a second.
            stops = stopped_tickets(state)
            picked, fallback = escalation_pick({**state, "gates": post["gates"]},
                                               self.resolve, self.rt, self.ticket_tiers,
                                               stopped_only=streak < 2)
            for tid in picked:
                why = (f"the wave check failed twice in a row ({failed})" if streak >= 2
                       else f"the wave check failed ({failed}) after the local session "
                            f"stopped early: {stops[tid]}")
                out.setdefault("gate_triage", []).append(GateTriage(
                    name=ESCALATE_REQUEST_TRIAGE, run_against=f"task/{tid}",
                    explanation=why[:2000],
                    produced_by=_BUILD_NODE,
                    seq=self.rt.seq(config, _BUILD_NODE, f"{ESCALATE_REQUEST_TRIAGE}:{tid}"),
                ))
                if fallback:
                    out["gate_triage"].append(GateTriage(
                        name=ESCALATE_FALLBACK_TRIAGE, run_against=f"task/{tid}",
                        explanation="no failure named a ticket: the whole wave escalates",
                        produced_by=_BUILD_NODE,
                        seq=self.rt.seq(config, _BUILD_NODE,
                                        f"{ESCALATE_FALLBACK_TRIAGE}:{tid}"),
                    ))
        if picked:
            # This check routes ESCALATE, not FIX: the rebuild is the ticket's extra
            # attempt and must leave the wave's fixer passes unspent.
            out.pop("budget", None)
        elif decision == FIX and not self.capped_off_target({**state, "gates": post["gates"]}) and (
                no_fix_progress(post, config, boundary=boundary)
                or _unchanged_tree_recheck(state, out, config, boundary=boundary)):
            [build] = [g for g in out["gates"] if g.name == "build"]
            out.setdefault("gate_triage", []).append(GateTriage(
                name=NO_FIX_PROGRESS_TRIAGE, run_against=build.run_against,
                explanation="the last fix pass left the same checks failing; the wave "
                            "stops fixing here instead of spending the remaining attempts",
                produced_by=_BUILD_NODE,
                seq=self.rt.seq(config, _BUILD_NODE, f"{NO_FIX_PROGRESS_TRIAGE}:"
                                                     f"{build.run_against}"),
            ))
        return out

    def _route(self, state: FactoryState, config: RunnableConfig, boundary: str) -> str:
        return route_after_gates(state, config, boundary=boundary,
                                 records=self.runner.records,
                                 max_fix_attempts=self.max_fix_attempts,
                                 total_cap=fix_cap(state, self.resolve, self.max_fix_attempts))

    def route(self, state: FactoryState, config: RunnableConfig) -> str:
        """:func:`route_after_gates`, except a pending escalation request routes
        :data:`ESCALATE`, and a fix pass that made no progress
        (:func:`no_fix_progress`, or the :data:`NO_FIX_PROGRESS_TRIAGE` ``run``
        recorded on this tree for a fix that changed nothing) halts as a spent bound
        does."""
        from foundry.agents.implementation.tickets import pending_escalations

        boundary = self._boundary(state)
        decision = self._route(state, config, boundary)
        if decision != ADVANCE and pending_escalations(state, self.resolve):
            return ESCALATE
        if decision == FIX and not self.capped_off_target(state) and (
                no_fix_progress(state, config, boundary=boundary)
                or self._stopped_on_current_tree(state, config)):
            return HALT
        return decision

    def capped_off_target(self, state: FactoryState) -> bool:
        """True iff the newest fix pass ran out of turns without editing any file
        the current failure names (:func:`failure_files`): it never reached the
        fix, so an unchanged failure is not "no progress" and the next pass runs,
        told to edit those files first. Still bounded: that pass spends a fix
        attempt like any other."""
        fixes = [a for a in _of_kind(state, FEATURE_DIFF_KIND)
                 if a.task_id and a.task_id.startswith("fix-")]
        if not fixes:
            return False
        prev = max(fixes, key=lambda a: a.seq)
        # proxy_agent's note for a session that hit packet.turn_budget.
        if not any(t.run_against == f"task/{prev.task_id}"
                   and t.explanation.startswith("turn budget exhausted")
                   for t in state.get("gate_triage") or ()):
            return False
        from foundry.agents.implementation.junior_engineer import _failed_gates

        named = set(failure_files(_failed_gates(state)))
        if not named:
            return False
        try:
            ops = _diff_ops(_resolve_typed(prev, self.resolve, FeatureDiff, FEATURE_DIFF_KIND),
                            self.resolve_blob)
        except MaterializationError:
            return False
        return not named & {*ops.writes, *ops.deletes, *ops.patches}

    @staticmethod
    def _stopped_on_current_tree(state: FactoryState, config: RunnableConfig) -> bool:
        builds = [g for g in state.get("gates") or () if g.name == "build"]
        if _fix_attempts(state, config) < 2 or not builds:
            return False
        tree = max(builds, key=lambda g: g.seq).run_against
        return any(t.name == NO_FIX_PROGRESS_TRIAGE and t.run_against == tree
                   for t in state.get("gate_triage") or ())


NO_FIX_PROGRESS_TRIAGE = "fix-no-progress"
