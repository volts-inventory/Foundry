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
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata as _im
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.agents._runtime import AgentRuntime
from foundry.gates.build import BuildGate
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import GateImpl, TwoZoneGateRunner
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
    "BUILD_TEST_SMOKE_BOUNDARY",
    "CRITERIA_CRITIQUE_KIND",
    "CRITERION_KIND",
    "FEATURE_DIFF_KIND",
    "FeatureDiffBodyError",
    "MaterializationError",
    "PathContainmentError",
    "flake_quarantined",
    "materialize_workdir",
    "route_after_gates",
    "run_build_test_gates",
    "run_spec_gate",
]

# Artifact.kind tags the upstream layers stamp on their handles — the seam we
# read the boundary's DATA inputs back off the artifacts channel by.
CRITERION_KIND = "acceptance_criterion"  # product layer (LEARNINGS QS3)
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
    current: dict[str, AcceptanceCriterion] = {}
    for a in sorted(_of_kind(state, CRITERION_KIND), key=lambda a: a.seq):
        c = _resolve_typed(a, resolve, AcceptanceCriterion, CRITERION_KIND)
        current[c.id] = c
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
    return {
        "criteria": criteria,
        "critique": {
            # RAW findings only — sorted for byte-stable tree_sha across folds.
            "security_abuse_omissions": sorted(critique.security_abuse_omissions),
        },
    }


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
    diff_artifacts = sorted(_of_kind(state, FEATURE_DIFF_KIND), key=lambda a: a.seq)
    for artifact in diff_artifacts:
        diff = _resolve_typed(artifact, resolve, FeatureDiff, FEATURE_DIFF_KIND)
        ops = _diff_ops(diff, resolve_blob)
        for rel, content in ops.writes.items():
            target = _contained_target(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel in ops.deletes:
            target = _contained_target(dest, rel)
            if target.is_file():
                target.unlink()
        for rel, hunks in ops.patches.items():
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
    return sorted(reqs)


def _scaffold_manifest(dest: Path) -> None:
    """Ensure ``dest`` ships a recognized dependency manifest for the AUDIT gate.

    No-op when the engineers already shipped one (audit theirs, untouched) or the
    tree is not Python. Otherwise writes a ``requirements.txt`` derived from the
    tree's real imports so the audit can ground its SBOM instead of fail-closing
    to ERROR for want of a manifest.
    """
    from foundry.gates.audit import recognized_manifests

    if recognized_manifests(dest):
        return
    reqs = _python_third_party_requirements(dest)
    if reqs is None:
        return
    header = (
        "# Generated by Foundry — pinned dependency manifest for the release.\n"
        "# Derived from the modules this project imports so the dependency/license\n"
        "# audit can ground its SBOM. Repin or extend as the code evolves.\n"
    )
    body = "".join(f"{line}\n" for line in reqs)
    (dest / "requirements.txt").write_text(header + body, encoding="utf-8")


# Files materialize_workdir writes for the gates themselves, not product code.
GATE_INPUT_FILES = frozenset({"criteria.json"})


def materialize_workdir(
    state: FactoryState, dest: Path, *, resolve: ArtifactResolver, resolve_blob: BlobResolver
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
    # Spec gate input: the criteria document, byte-stable across folds.
    (dest / "criteria.json").write_text(
        as_text(_criteria_payload(state, resolve)), encoding="utf-8"
    )
    # Build / Test gate inputs: the integrated code + test sources.
    _write_sources(dest, state, resolve, resolve_blob)
    # Guarantee an auditable dependency manifest so the AUDIT gate can ground its
    # SBOM (no-op when the engineers already shipped one, or the tree is non-Python).
    _scaffold_manifest(dest)
    # prefer_git=False: dest is a fresh materialized tree, not a git worktree.
    return tree_sha(dest, toolchain_digest=_TOOLCHAIN_DIGEST, prefer_git=False)


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
    """Run the Build + Test (+ profile Smoke) gates at an implementation /
    quality exit boundary.

    Materializes the integrated tree (code + test sources + criteria.json) into
    ``workdir`` ONCE and runs :class:`BuildGate` then :class:`TestGate` against
    its ``tree_sha`` (the same integrated digest, so both verdicts latch/bind to
    one state). ``min_passed`` is the Test gate's adequacy floor — a vacuous
    (0-test) suite is FAIL, never PASS (governance §1c). Returns
    ``{"gates": [<attested build>, <attested test>]}``.

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
    build_cmd = profile.build_cmd if profile is not None else None
    test_cmd = profile.test_cmd if profile is not None else None
    build = _run_gate(
        BuildGate(cmd=build_cmd, seal_key=runner.seal_key),
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
    test = _run_gate(
        TestGate(
            cmd=test_cmd,
            min_passed=effective_min,
            discriminator=discriminator,
            seal_key=runner.seal_key,
        ),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_TEST_NODE,
    )
    gates = [build, test]
    if profile is not None and profile.smoke_cmd is not None:
        from foundry.gates.smoke import SmokeGate

        gates.append(
            _run_gate(
                SmokeGate(profile.smoke_cmd, seal_key=runner.seal_key),
                rt=rt,
                runner=runner,
                config=config,
                run_against=run_against,
                workdir=workdir,
                node_id=_SMOKE_NODE,
            )
        )
    return {"gates": gates}


# ---- routing predicate (fail-closed, convergence-bounded) -------------------

# Which gate verdicts each phase boundary is responsible for.
SPEC_BOUNDARY = "spec"
BUILD_TEST_BOUNDARY = "build_test"
# Profile boundary variant (ROADMAP Phase 3): when the run's verification
# profile pins a smoke (runtime boot) command, the boundary is also responsible
# for the "smoke" verdict — a missing/FAILed smoke can never advance.
BUILD_TEST_SMOKE_BOUNDARY = "build_test_smoke"

_BOUNDARY_GATES: dict[str, frozenset[str]] = {
    SPEC_BOUNDARY: frozenset({"spec"}),
    BUILD_TEST_BOUNDARY: frozenset({"build", "test"}),
    BUILD_TEST_SMOKE_BOUNDARY: frozenset({"build", "test", "smoke"}),
}

# Routing outcomes.
ADVANCE = "advance"  # all relevant gates PASS — proceed to the next phase
FIX = "fix"  # a FAIL/ERROR within the convergence bound — back to the layer
HALT = "halt"  # bound exhausted or a stable-FAIL latch — escalate / stop


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


def route_after_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int = 3,
) -> str:
    """Route a phase boundary on its relevant gate verdicts. Fail-closed.

    Returns:

    * :data:`ADVANCE` — every relevant gate has a PASS verdict on a non-disqualified
      digest; proceed to the next phase.
    * :data:`FIX` — a FAIL / (non-flake) ERROR, a missing relevant verdict, OR a
      stable-FAIL-latched digest, WITHIN the convergence bound; route back to the
      responsible layer to fix FORWARD into a new tree.
    * :data:`HALT` — the ``max_fix_attempts`` convergence bound is exhausted (escalate
      with the best draft); a non-fixable terminal condition.

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

    bound_spent = _fix_attempts(state, config) >= max_fix_attempts

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
