"""Materialize the integrated tree the gates run against from the
content-addressed artifacts on ``state``."""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from importlib import metadata as _im
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.base_tree import base_features, copy_base_tree
from foundry.canonical import content_digest
from foundry.gates.profiles import property_testing
from foundry.gates.pydeps import (
    SCAFFOLD_HEADER,
    _dist_for,
    _host_distributions,
    _import_declared,
    _normalized,
    requirement_name,
)
from foundry.graph.gate_wiring.diffs import (
    FeatureDiffBodyError,
    MaterializationError,
    PathContainmentError,
    _apply_hunks,
    _contained_target,
    _diff_ops,
)
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    FEATURE_DIR,
    SKELETON_TASK_ID,
    FeatureDiff,
    StackDecision,
    Ticket,
    feature_path,
)
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from collections.abc import Callable

    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.state.factory_state import FactoryState

    # The SAME resolver seam senior_engineer uses (LEARNINGS QS3 / IM4):
    # reconstitute a TYPED out-of-band body from its content-addressed Artifact
    # handle (AcceptanceCriterion / FeatureDiff) -> its FrozenModel. The return
    # is ``Any`` (mirrors senior_engineer.ArtifactResolver) but here we only ever
    # resolve typed bodies through it — the raw diff BODY rides a SEPARATE seam.
    ArtifactResolver = Callable[[Artifact], Any]


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


def _criteria_list(state: FactoryState, resolve: ArtifactResolver) -> list[dict[str, Any]]:
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


def _latest_critique(state: FactoryState, resolve: ArtifactResolver) -> CriteriaCritique | None:
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


# Never shipped: the harness's own plugins (a test never imports them) and pure dev
# tools. A library the shipped tests import IS shipped, the harness's included.
_DEV_TOOLS = frozenset(
    {"pluggy", "xdist", "pytest_cov", "coverage"}
    | {"mypy", "mypy_extensions", "ruff", "black", "flake8", "pylint", "isort"}
)


def _harness_test_libraries() -> tuple[str, ...]:
    """Import names of what the gate harness provides that shipped tests import: the
    pytest runner, pytest-bdd and the Python profile's property-testing library. The
    gates run Foundry's copies, so a manifest without them passes every gate yet
    ships tests that cannot be collected."""
    lib = property_testing("python")
    return ("pytest", "_pytest", "pytest_bdd", *((lib.library,) if lib else ()))


def _imported_modules(dest: Path, py_files: list[Path] | None = None) -> set[str]:
    """The top-level module names the tree's ``.py`` files import (absolute imports)."""
    imported: set[str] = set()
    for path in dest.rglob("*.py") if py_files is None else py_files:
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
    return imported


def _python_third_party_requirements(dest: Path) -> list[str] | None:
    """Requirement lines derived from the tree's real imports, or ``None`` when
    ``dest`` is not a Python tree (no ``.py`` — nothing to scaffold here).

    The third-party top-level modules the app and its tests import (stdlib, in-tree
    local modules, what the gate harness provides and dev tools excluded), mapped
    to their installed distribution + pinned version when resolvable.
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
    imported = _imported_modules(dest, py_files)
    third_party = {
        n for n in imported if n and n not in stdlib and n not in local and n not in _DEV_TOOLS
    }
    try:
        pkg_dist = _im.packages_distributions()
    except Exception:
        pkg_dist = {}
    reqs: set[str] = set()
    for name in third_party:
        dists = pkg_dist.get(name)
        if dists:
            reqs.update(_pin(d) for d in dists if not d.lower().startswith("types-"))
        else:
            reqs.add(name)  # unresolved import: list the bare name, still auditable
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


def _ship_test_manifest(dest: Path) -> None:
    """Append to the shipped ``requirements.txt`` (at Foundry's pin) each library the
    harness provides that the tests import and the manifest omits, so the shipped
    tests collect outside the gates. Never drops a line: a package can be loaded with
    no import (a server, a DB driver, a plugin), and a wrong drop breaks the app."""
    manifest = dest / "requirements.txt"
    if not manifest.is_file():
        return  # none (the scaffold derives one from imports) or pyproject-only
    text = manifest.read_text(encoding="utf-8")
    imported = _imported_modules(dest)
    installed = _host_distributions()
    kept = text.splitlines()
    declared: set[str] = set()
    for line in kept:
        spec = re.sub(r"(^|\s)#.*$", "", line).strip()
        dist = requirement_name(spec) if spec and not spec.startswith("-") else None
        if dist is not None:
            declared.add(_normalized(dist))
    added = [
        _pin(_dist_for(lib, installed))
        for lib in _harness_test_libraries()
        if lib in imported and not _import_declared(lib, declared, installed)
    ]
    out = "".join(f"{line}\n" for line in [*kept, *dict.fromkeys(added)])
    if out != text:
        manifest.write_text(out, encoding="utf-8")


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


def _wave0_ticket_ids(state: FactoryState, resolve: ArtifactResolver) -> tuple[set[str], set[str]]:
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
    # The fixer's FeatureDiffs (make_fixer_node), the restores of a fix loop's best
    # tree (keep_best), and a ticket restart's reset and rebuild: all repair the
    # tree within one fix loop, none is a later wave's ticket.
    return task_id.startswith(("fix-", REVERT_PREFIX, RESTART_PREFIX, RESET_PREFIX))


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
    diffs = [
        (a, _resolve_typed(a, resolve, FeatureDiff, FEATURE_DIFF_KIND))
        for a in _of_kind(state, FEATURE_DIFF_KIND)
    ]
    if not any(d.task_id == CONTRACTS_TICKET_ID for _, d in diffs):
        return  # not built yet: nothing agreed to restore
    wave0, known = _wave0_ticket_ids(state, resolve)
    frozen_at = min(
        (
            a.seq
            for a, d in diffs
            if d.task_id not in wave0
            and d.task_id != SKELETON_TASK_ID
            and (d.task_id in known or not _is_fix(d.task_id))
        ),
        default=None,
    )
    owned = [a for a, _ in diffs if frozen_at is None or a.seq < frozen_at]
    fixes = [
        a
        for a, d in diffs
        if frozen_at is not None
        and a.seq >= frozen_at
        and _is_fix(d.task_id)
        and d.task_id not in known
    ]
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
    state: FactoryState,
    dest: Path,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
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
    if scaffold_manifest:
        return _gated_tree_sha(dest, state)
    return tree_sha(dest, toolchain_digest=_toolchain_digest(state.get("stack")))


def _gated_tree_sha(dest: Path, state: FactoryState) -> str:
    stack = state.get("stack")
    if stack is None or stack.language == "python":
        _scaffold_manifest(dest)
        _ship_test_manifest(dest)
    # The stack's commands are part of what a verdict judges, so they join the
    # toolchain digest: the stable-FAIL latch keys on run_against, and a re-planned
    # stack must be able to re-gate the same source. No stack keeps the old digest.
    return tree_sha(dest, toolchain_digest=_toolchain_digest(stack))


def _text_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        if not f.is_file() or rel in GATE_INPUT_FILES:
            continue
        try:
            out[rel] = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a base tree's binary asset: the sandbox clone has it
    return out


def integrated_files(
    state: FactoryState, *, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> dict[str, str]:
    """The integrated tree's text files, scaffold-free and without the gate inputs
    (an agent told to "make the gates pass" must not be handed the oracle to edit):
    what an agent's worktree is seeded with, and what a fix pass's snapshot keeps.
    Raises :class:`MaterializationError` when the tree cannot be built."""
    return integrated_tree(state, resolve=resolve, resolve_blob=resolve_blob)[1]


def integrated_tree(
    state: FactoryState, *, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> tuple[str, dict[str, str]]:
    """From one build: the ``tree_sha`` the gates judge for the integrated tree
    (:func:`materialize_workdir`'s) and its :func:`integrated_files`."""
    with tempfile.TemporaryDirectory(prefix="foundry-tree-") as tmp:
        root = Path(tmp)
        materialize_workdir(
            state, root, resolve=resolve, resolve_blob=resolve_blob, scaffold_manifest=False
        )
        files = _text_files(root)
        return _gated_tree_sha(root, state), files


def lift_epoch(state: FactoryState, seq: Seq) -> Seq:
    """``seq`` with its epoch past every FeatureDiff on ``state``: integration orders
    diffs by Seq, and a fresh diff key starts at epoch 1, tying with (and possibly
    sorting under) the diffs it repairs."""
    top = max((a.seq.emission_epoch for a in _of_kind(state, FEATURE_DIFF_KIND)), default=0)
    return seq._replace(emission_epoch=top + 1)


def _toolchain_digest(stack: StackDecision | None) -> str:
    if stack is None:
        return _TOOLCHAIN_DIGEST
    return f"{_TOOLCHAIN_DIGEST}+stack:" + content_digest(stack.model_dump(mode="json"))


def planned_files(state: FactoryState, resolve: ArtifactResolver) -> list[str]:
    """Every ticket's planned files, built or not."""
    return [
        f
        for a in _of_kind(state, TICKET_KIND)
        for f in _resolve_typed(a, resolve, Ticket, TICKET_KIND).files
    ]


# A restore of a fix loop's best tree (keep_best).
REVERT_PREFIX = "revert-"
# A ticket restart (plan_next_pass): the reset of the tickets' files to the tree
# before they were built, then each ticket's rebuild, both inside the fix loop.
RESTART_PREFIX = "restart-"
RESET_PREFIX = "reset-"
