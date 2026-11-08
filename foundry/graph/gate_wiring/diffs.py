"""Parse a FeatureDiff body (addition diff, unified patch or snapshot) into the
file operations the materializer applies, fail-closed on anything it cannot read."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from foundry.schemas.implementation import FeatureDiff

if TYPE_CHECKING:
    from collections.abc import Callable

    # The DISTINCT blob-resolve seam keyed on the ``cas://`` diff_uri (LEARNINGS
    # D-R1-01/02). A FeatureDiff's diff body is stored out-of-band as a raw blob
    # via ``sandbox.put_blob`` (a UNIFIED-DIFF string) and read back via
    # ``sandbox.get_blob(uri) -> bytes``. We resolve it through THIS seam — keyed
    # on the uri, not the typed Artifact handle — so a kind-aware CAS resolver
    # cannot mis-dispatch the diff body as a typed artifact. The factory_graph
    # wave backs it with ``sandbox.get_blob``.
    BlobResolver = Callable[[str], "bytes | str"]


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
    "diff --git ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
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
                f"FeatureDiff body for {diff_uri!r}: '--- {old}' not followed by a '+++' header"
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
                    f"FeatureDiff body for {diff_uri!r}: modification of {new_rel!r} has no hunks"
                )
            patches[_require_rel(diff_uri, new_rel)] = tuple(hunks)
        else:
            raise FeatureDiffBodyError(
                f"FeatureDiff body for {diff_uri!r}: '--- /dev/null' + "
                f"'+++ /dev/null' section is meaningless"
            )
    if not saw_section:
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff_uri!r} parsed to no file sections — empty/invalid diff"
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
                f"FeatureDiff body for {diff_uri!r}: overlapping/unordered hunks for {rel!r}"
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
            f"diff body path {rel!r} resolves outside the integrated tree — rejected fail-closed"
        )
    return target
