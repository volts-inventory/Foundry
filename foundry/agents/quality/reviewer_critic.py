"""Review verdict — the shared ``rt.proxy.judge`` call the quality reviewer makes (T1).

SOP: review the diff for correctness, design adherence, readability, and the
*test* diffs (guard against weakened assertions) → emit a verdict
``approve | request-changes`` with specific findings.

Trust model (LEARNINGS L5 / AR4): the reviewer is the ONE quality node that
touches the ``gates`` channel — but it never *mints* a verdict. It calls
``rt.proxy.judge(...)``, and the proxy:

* extracts ``status`` from the grammar-constrained verdict channel (a
  compromised adapter cannot have free-text "approve" authentically signed),
* stamps the judgment binding OUTSIDE the adapter via the injected ZoneBSigner
  (the agent holds no key), and
* returns a :class:`JudgmentResult` whose ``.gate_result`` already carries the
  binding + ``judgment_provenance`` stamp.

The node writes that proxy-stamped ``GateResult`` to ``gates``. ``_merge_gates``
admits it only because it carries a valid binding + an unconsumed server-issued
``verdict_nonce`` + no negative latch — none of which the agent forged. L5 is
honored: the agent emitted a verdict it could never have signed itself.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import (
    ArtifactResolver,
    journal_entry,
    require_verdict_nonce,
    server_issued_nonce,
    state_sha,
    subject_artifact_uri,
)
from foundry.durability.artifact_store import UnknownArtifactError
from foundry.proxy.model_provider_proxy import OutputLimitReached
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.implementation import FEATURE_DIR, Effort, FeatureDiff
from foundry.schemas.product import PRD
from foundry.schemas.quality import ReviewedTree

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.proxy.model_provider_proxy import JudgmentResult
    from foundry.schemas.base import Artifact
    from foundry.schemas.journal import JournalEntry
    from foundry.schemas.product import AcceptanceCriterion
    from foundry.state.factory_state import FactoryState

_GATE_NAME = "review"  # a JUDGMENT_VERDICTS gate (reducers.py)

# The code-review JUDGE runs every release AND every fix-loop iteration, so its
# tier is a recurring cost. Sonnet (T2) is a strong reviewer; that's the default
# cost/value pick (tiers are a cost choice — arch §8). Restore Opus review with
# FOUNDRY_REVIEW_TIER=T1 when you want the heaviest reasoning on the review gate.
import os as _os  # noqa: E402

_REVIEW_TIER = _os.environ.get("FOUNDRY_REVIEW_TIER", "T2").strip() or "T2"

# The kind the Junior Engineer stamps on the diff handle it emits to ``artifacts``
# (foundry/agents/implementation/junior_engineer.py) — the artifact this reviewer
# judges when the runner has not pinned an explicit subject on the config.
_FEATURE_DIFF_KIND = "feature_diff"
# The snapshot of the tree a verdict judged (reviewer.py emits it), so the next
# review in the run judges only the change since.
REVIEWED_TREE_KIND = "reviewed_tree"


def _newest_feature_diff_uri(artifacts: Sequence[Artifact]) -> str | None:
    """The diff under review: the newest-by-``Seq`` ``feature_diff`` handle, if any.

    C-R1-03: the channel folds by ``merge_by_uri`` (insertion-order, NOT seq), so
    ``artifacts[-1]`` is not deterministically the diff under review. Select by
    KIND and pick the latest by ``Seq`` (mirrors ``senior_engineer._first_of_kind``)
    so a supersession (higher-``seq`` handle under a fresh uri, L4) deterministically
    wins. Returns ``None`` when no diff handle is present (the config-pinned subject
    is the primary source; the node fails closed if neither yields one)."""
    candidates = [a for a in artifacts if getattr(a, "kind", None) == _FEATURE_DIFF_KIND]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.seq).uri


def _attested_tree_sha(state: FactoryState) -> str | None:
    """The REAL integrated ``tree_sha`` the deterministic gates ran against, read
    off the newest-by-``Seq`` attested verdict on the ``gates`` channel.

    Every ``GateResult`` carries the TRUE ``run_against`` of the integrated tree it
    was evaluated on (``materialize_workdir`` → ``tree_sha``; GW2 binds the release
    gates to ONE tree). Used as the reviewer's ``state_sha`` fallback so that when
    the runner does NOT pin a synthetic ``state_sha`` label (real backends), the
    verdict's ``run_against`` honestly names the tree that was actually built —
    rather than a bare run id. ``None`` when no verdict carries one yet (e.g. the
    reviewer runs before the build gates), in which case the caller falls back to
    a stable per-run label. Config-pin precedence means the ``mock`` smoke (which
    pins ``state_sha``) is unaffected."""
    best: Any = None
    for g in state.get("gates") or ():
        if getattr(g, "run_against", "") and (best is None or g.seq > best.seq):
            best = g
    return str(best.run_against) if best is not None else None


def failing_checks(state: FactoryState, tree: str | None) -> tuple[tuple[str, ...], ...]:
    """What a review finding may cite as failing: ``(gate names, test ids)`` of the
    newest verdict per gate that ran on ``tree`` (the tree under review) and
    FAILed or ERRORed, findings or not, with the test ids and scenario refs their
    findings name. Judgment verdicts are left out, so a reviewer can never back a
    finding with an earlier review's own FAIL.

    The reviewer runs BEFORE the final build/test/smoke gates: on a fix pass every
    verdict on state judged the pre-fix tree, and a failing wave check never
    reaches review. Usually nothing has run on ``tree``, so a citation alone
    cannot block; the Test gate right after judges those checks itself."""
    from foundry.graph.gate_wiring import failing_ids
    from foundry.state.reducers import JUDGMENT_VERDICTS

    latest: dict[str, GateResult] = {}
    for g in state.get("gates") or ():
        if (
            g.run_against == tree
            and g.name not in JUDGMENT_VERDICTS
            and (g.name not in latest or g.seq > latest[g.name].seq)
        ):
            latest[g.name] = g
    failed = [
        g
        for g in sorted(latest.values(), key=lambda g: g.seq)
        if g.status in (GateStatus.FAIL, GateStatus.ERROR)
    ]
    tests = dict.fromkeys(i for g in failed for i in failing_ids(g.findings))
    return tuple(g.name for g in failed), tuple(tests)


def _reviewed_tree(
    state: FactoryState, resolve: ArtifactResolver | None, resolve_blob: BlobResolver | None
) -> str | None:
    """The ``tree_sha`` the final gates will judge for the change under review, or
    ``None`` when it cannot matter (no gate verdict on state failed) or cannot be
    built here (no resolvers). Materializing is the only way to know it: verdict
    ``Seq``s are not a causal order across nodes."""
    if (
        resolve is None
        or resolve_blob is None
        or not any(
            g.status in (GateStatus.FAIL, GateStatus.ERROR) for g in state.get("gates") or ()
        )
    ):
        return None
    from foundry.graph.gate_wiring import materialize_workdir

    with tempfile.TemporaryDirectory(prefix="foundry_review_tree_") as tmp:
        return materialize_workdir(state, Path(tmp), resolve=resolve, resolve_blob=resolve_blob)


# Keeps the review request inside a sane context/cost budget on large builds.
_DIFF_CHAR_CAP = 60_000


# A template, whatever the stack: a templates/views directory or a template file.
_TEMPLATE_PATH = re.compile(
    r"(^|/)(templates?|views?)/|\.(html?|jinja2?|j2|hbs|ejs|tmpl|erb|mustache|njk|liquid)$"
)


def _file_sections(diff: str) -> list[tuple[str, str]]:
    """``diff`` split per file: (section text, its path or "")."""
    splitter = _DIFF_FILE_SPLIT if _DIFF_FILE_SPLIT.search(diff) else _HUNK_FILE_SPLIT
    sections = [x for x in splitter.split(diff) if x.strip()]
    return [(x, m.group(1) if (m := _DIFF_PATH.search(x)) else "") for x in sections]


def _capped(text: str, keep: Callable[[str], bool] | None = None) -> tuple[str, bool]:
    """``text`` cut to :data:`_DIFF_CHAR_CAP` with a ``[TRUNCATED]`` note, and
    whether it fit whole. A cut keeps the files ``keep`` names (the entrypoint and
    the templates) in front, whole up to half the cap: a review that missed the
    composition root and the templates passed their bugs."""
    if len(text) <= _DIFF_CHAR_CAP:
        return text, True
    pinned = ""
    for x, path in _file_sections(text):
        # Whole files only: one too big to fit is skipped, the smaller ones still go.
        if path and keep and keep(path) and len(pinned) + len(x) <= _DIFF_CHAR_CAP // 2:
            pinned += x
    head = ""
    if pinned:
        head = (
            "[The change is truncated below; the entrypoint and template files come "
            "first, in full:]\n" + pinned + "\n\n[The whole change, in order:]\n"
        )
    room = max(_DIFF_CHAR_CAP - len(head), _DIFF_CHAR_CAP // 2)
    note = f"\n\n[TRUNCATED: showing the first {room} of {len(text)} chars.]"
    return head + text[:room] + note, False


def _always_shown(rt: AgentRuntime, state: FactoryState) -> Callable[[str], bool]:
    """Whether a changed path is one the review must always see: a file of an
    entrypoint ticket (the composition root) or a template."""
    entry: set[str] = set()
    if rt.cas is not None:
        from foundry.agents.implementation.tickets import read_tickets

        try:
            tickets = read_tickets(state, rt.cas.resolve)
        except Exception:
            tickets = ()
        entry = {f.removeprefix("./") for t in tickets if t.entrypoint for f in t.files}
    return lambda path: path in entry or bool(_TEMPLATE_PATH.search(path))


def _diff_text(
    rt: AgentRuntime, state: FactoryState, keep: Callable[[str], bool] | None = None
) -> tuple[str, bool]:
    """The change under review as unified-diff text: every ``feature_diff`` body in
    ``Seq`` order (the order ``materialize_workdir`` applies them), capped at
    :data:`_DIFF_CHAR_CAP`, and whether that text is the whole change (nothing cut
    or unresolvable). The judge cannot dereference a ``cas://`` uri, so without
    this it sees only a handle and can never approve."""
    handles = sorted(
        (a for a in (state.get("artifacts") or ()) if a.kind == _FEATURE_DIFF_KIND),
        key=lambda a: a.seq,
    )
    if not handles:
        return "(No feature_diff artifacts on state: there is no diff text to review.)", False
    if rt.cas is None:
        return "(No artifact store is wired: the diff text could not be resolved.)", False
    parts: list[str] = []
    missing: list[str] = []
    for handle in handles:
        try:
            body = rt.cas.resolve(handle)
            if not isinstance(body, FeatureDiff):
                missing.append(handle.uri)
                continue
            text = rt.cas.resolve_blob(body.diff_uri).decode("utf-8", errors="replace")
        except UnknownArtifactError:
            missing.append(handle.uri)
            continue
        parts.append(f"--- task {handle.task_id or body.task_id} ({handle.uri}) ---\n{text}")
    if not parts:
        return "(None of the feature_diff bodies could be resolved: no diff text to review.)", False
    out, whole = _capped("\n\n".join(parts), keep)
    if missing:
        out += "\n\n[Unresolvable diffs, not shown: " + ", ".join(missing) + "]"
    return out, whole and not missing


def review_tree(
    state: FactoryState, resolve: ArtifactResolver | None, resolve_blob: BlobResolver | None
) -> tuple[str, dict[str, str]] | None:
    """The tree under review from one build: its gated ``tree_sha`` and its text
    files, or ``None`` when it cannot be built here (no resolvers, or a body the
    materializer rejects): the review then falls back to judging the feature
    diffs whole."""
    if resolve is None or resolve_blob is None:
        return None
    from foundry.graph.gate_wiring import MaterializationError, integrated_tree

    try:
        return integrated_tree(state, resolve=resolve, resolve_blob=resolve_blob)
    except (MaterializationError, UnknownArtifactError):
        return None


def _prior_review(
    state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> tuple[GateResult, dict[str, str]] | None:
    """The run's newest review verdict and the files of the tree it judged, or
    ``None`` before the first review, when that review did not see its whole
    change (truncated, or recorded before completeness was), or when that tree's
    snapshot is not on state or no longer resolves (a resume without the store):
    the review is then full."""
    from foundry.graph.gate_wiring import MaterializationError, snapshot_files

    reviews = [g for g in state.get("gates") or () if g.name == _GATE_NAME]
    if not reviews:
        return None
    prior = max(reviews, key=lambda g: g.seq)
    for handle in state.get("artifacts") or ():
        if handle.kind != REVIEWED_TREE_KIND:
            continue
        try:
            body = resolve(handle)
        except UnknownArtifactError:
            continue
        if isinstance(body, ReviewedTree) and body.verdict_seq == prior.seq:
            if not body.complete:
                return None
            try:
                return prior, snapshot_files(body.snapshot_uri, resolve_blob)
            except (UnknownArtifactError, MaterializationError):
                return None
    return None


def _delta_text(
    before: Mapping[str, str],
    after: Mapping[str, str],
    keep: Callable[[str], bool] | None = None,
) -> tuple[str, bool]:
    """The change from ``before`` to ``after`` as unified diffs, path by path,
    capped like :func:`_diff_text`, and whether it fit whole."""
    parts: list[str] = []
    for path in sorted({*before, *after}):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        parts.append(
            "".join(
                difflib.unified_diff(
                    (old or "").splitlines(keepends=True),
                    (new or "").splitlines(keepends=True),
                    fromfile=f"a/{path}" if old is not None else "/dev/null",
                    tofile=f"b/{path}" if new is not None else "/dev/null",
                )
            )
        )
    if not parts:
        return "(No file changed since the tree you reviewed.)", True
    return _capped("\n".join(parts), keep)


_SOP = (
    "You are the Reviewer/Critic, the evaluator side of the evaluator-optimizer "
    "loop. Review the diff for correctness, design adherence, readability, and "
    "scrutinize the TEST diffs for weakened assertions. Return a verdict: "
    "approve only if the change is correct and the tests still bind the "
    "criteria; otherwise request-changes with specific, actionable findings. "
    "A finding blocks only if it cites real evidence: a check that is failing in "
    "this tree's gate results (listed in the request) in failing_check, or a "
    "reproducible command with correct_result (what a CORRECT app returns, not "
    "your prediction of what this one does) and a DIFFERENT observed_result (what "
    "this app actually returned when you ran it). An exception, traceback, 5xx or "
    "crash you observed is a defect: never copy it into correct_result. Citing a "
    "passing test or scenario does not count. Opinions on "
    "test quality (fragile asserts, missing edge cases, needless mocks) are "
    "advisory while every check passes: record them, they do not block. "
    "Work through this checklist against the spec, the contracts and the launch "
    "result in the request, and for each problem you find give a concrete "
    "reproduction: a shell command (curl against the launched app, a CLI call, a "
    "python -c one-liner) with its correct_result and its observed_result. "
    "(1) Structure: logic duplicated across files, dead modules nothing imports, an "
    "entry point that re-implements what another module owns. "
    "(2) Spec coverage: every edge case the spec names, and BOTH branches of every "
    "'X or Y' requirement. "
    "(3) Security: hardcoded or fallback secret keys; POST forms without CSRF "
    "protection; SQL built by string formatting, and LIKE patterns that do not "
    "escape the user's % and _; passwords hashed with a fast digest (md5/sha*) "
    "instead of a password KDF; routes reachable without the auth or role the spec "
    "requires; request bodies and uploads with no size limit. "
    "(4) Persistence: one database connection shared across server threads (sqlite3 "
    "without check_same_thread, or no lock), an in-memory database holding data "
    "that must persist, and multi-step writes that are not one transaction. "
    "(5) Spec workarounds: code that special-cases one of the spec's literal examples "
    "(a branch or input rewrite keyed on that exact value) or works around a "
    "contradiction in the spec instead of implementing the general rule is a "
    "BLOCKING finding even when every check passes. Reproduce it: a neighbouring "
    "input the workaround breaks (correct vs observed result), or a grep for the "
    "special-cased literal (correct_result: no match; observed_result: the file and "
    "line). "
    "(6) Pinned constraints and must-haves: each one listed in the request is a "
    "criterion. Try to break it with hostile input the scenarios never use: a "
    "10,000-character argument, 2,000 nested brackets, a 5,000-digit number, a long run "
    "of a repeated operator or sign, an empty string, an oversized form field. A "
    "violation (e.g. a traceback, panic or crash where a constraint forbids one) is "
    "BLOCKING: give the command, the constraint's id as correct_result's basis, and the "
    "observed output. "
    "Report at most 10 findings, most important first, each in a sentence or two; "
    "never repeat a finding."
)

# The local reviewer's output budget: thinking at low effort plus a verdict of at
# most 10 findings fits well inside it; a reply that runs past it is rambling.
_REVIEW_MAX_TOKENS = 24_576
_REVIEW_EFFORT: Effort = "low"
_SPEC_CHAR_CAP = 12_000
_CONTRACT_CHAR_CAP = 8_000
_LAUNCH_FINDINGS = 20
# The launch gate (foundry/gates/launch.py) is read by name only: the reviewer never
# depends on its module.
_LAUNCH_GATE = "launch"
_INTERFACE_CONTRACT_KIND = "interface_contract"


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[TRUNCATED: first {cap} of {len(text)} chars]"


def _spec_text(rt: AgentRuntime, state: FactoryState) -> str:
    """The spec the build answers to: the design spec (else the request), then the
    newest PRD's prose body when the store holds it."""
    parts = [str(state.get("spec") or state.get("request") or "").strip()]
    prds = [a for a in state.get("artifacts") or () if a.kind == "prd"]
    if prds and rt.cas is not None:
        try:
            prd = rt.cas.resolve(max(prds, key=lambda a: a.seq))
            body = rt.cas.get_body(prd.prd_uri) if isinstance(prd, PRD) else None
        except Exception:
            body = None
        text = str(getattr(body, "text", "") or "").strip()
        if text:
            parts.append("PRD:\n" + text)
    return _clip("\n\n".join(p for p in parts if p), _SPEC_CHAR_CAP)


def _contract_text(rt: AgentRuntime, state: FactoryState) -> str:
    """The planned contracts, newest record per name: each signature, else its
    authored prose body."""
    if rt.cas is None:
        return ""
    newest: dict[str, tuple[Any, InterfaceContract]] = {}
    for handle in state.get("artifacts") or ():
        if handle.kind != _INTERFACE_CONTRACT_KIND:
            continue
        try:
            body = rt.cas.resolve(handle)
        except Exception:
            continue
        if isinstance(body, InterfaceContract) and (
            body.name not in newest or handle.seq > newest[body.name][0]
        ):
            newest[body.name] = (handle.seq, body)
    lines: list[str] = []
    for name in sorted(newest):
        c = newest[name][1]
        text = c.signature.strip()
        if not text:
            try:
                text = str(getattr(rt.cas.get_body(c.spec_uri), "text", "") or "").strip()
            except Exception:
                text = ""
        lines.append(
            f"- {name}" + (f" [{c.kind}]" if c.kind else "") + (f": {text}" if text else "")
        )
    return _clip("\n".join(lines), _CONTRACT_CHAR_CAP)


def _plan_rules_text(rt: AgentRuntime, state: FactoryState) -> str:
    """The plan's live pinned constraints and the newest brief's must-haves, one
    per line with its id: no check verifies them, so the review must."""
    from foundry.schemas.product import IntentBrief
    from foundry.state.reducers import live_constraints

    lines = [
        f"- {c.id} (pinned constraint): {c.text}"
        for c in sorted(
            live_constraints(list(state.get("pinned_constraints") or ())), key=lambda c: c.id
        )
    ]
    briefs = [a for a in state.get("artifacts") or () if a.kind == "intent_brief"]
    if briefs and rt.cas is not None:
        try:
            brief = rt.cas.resolve(max(briefs, key=lambda a: a.seq))
        except Exception:
            brief = None
        if isinstance(brief, IntentBrief):
            ids = brief.must_have_ids
            lines += [
                f"- {ids[i] if i < len(ids) else f'MH-{i + 1}'} (must-have): {text}"
                for i, text in enumerate(brief.must_haves)
            ]
    return _clip("\n".join(lines), _SPEC_CHAR_CAP)


def launch_result(state: FactoryState) -> GateResult | None:
    """The newest launch-gate verdict on state, if the launch gate has run."""
    launches = [g for g in state.get("gates") or () if g.name == _LAUNCH_GATE]
    return max(launches, key=lambda g: g.seq) if launches else None


def _launch_text(state: FactoryState) -> str:
    g = launch_result(state)
    if g is None:
        return "not run yet on this build."
    shown = "\n".join(f"  {f}" for f in g.findings[:_LAUNCH_FINDINGS])
    return f"{g.status.value} (tree {g.run_against})" + (f":\n{shown}" if shown else ".")


_DIFF_FILE_SPLIT = re.compile(r"^(?=diff --git )", re.MULTILINE)
_HUNK_FILE_SPLIT = re.compile(r"^(?=--- (?:a/|/dev/null))", re.MULTILINE)
_DIFF_PATH = re.compile(r"^(?:\+\+\+ b/|--- a/)(\S+)", re.MULTILINE)
_TEST_PATH = re.compile(r"(^|/)(tests?|features|steps|__tests__)/|(^|/)(test_[^/]*|conftest\.py)$")


def _failing_findings(state: FactoryState) -> str:
    """Every finding of the newest non-judgment verdict per gate that failed."""
    from foundry.state.reducers import JUDGMENT_VERDICTS

    latest: dict[str, GateResult] = {}
    for g in state.get("gates") or ():
        if g.name not in JUDGMENT_VERDICTS and (g.name not in latest or g.seq > latest[g.name].seq):
            latest[g.name] = g
    return "\n".join(
        f
        for g in latest.values()
        if g.status in (GateStatus.FAIL, GateStatus.ERROR)
        for f in g.findings
    )


def trimmed_diff(diff: str, failing: str, keep: Callable[[str], bool] | None = None) -> str:
    """``diff`` cut to the files the failing checks name, else to its non-test
    source files, within half the usual cap: the retry after a reply ran past the
    output cap. The files ``keep`` names (entrypoint, templates) come first."""
    paths = _file_sections(diff)
    sections = [s for s, _ in paths]
    named = [s for s, p in paths if p and p in failing]
    kept = named or [s for s, p in paths if p and not _TEST_PATH.search(p)] or sections
    first = [s for s, p in paths if p and keep is not None and keep(p)]
    kept = list(dict.fromkeys([*first, *kept]))
    text = "".join(kept)
    cap = _DIFF_CHAR_CAP // 2
    if len(text) > cap:
        text = text[:cap] + f"\n[TRUNCATED: first {cap} of {len(text)} chars]"
    which = "the files the failing checks name" if named else "the changed source files"
    return f"[Trimmed to {which} after your last reply ran past the output limit.]\n" + text


def review(
    rt: AgentRuntime,
    state: FactoryState,
    config: RunnableConfig,
    *,
    node_id: str,
    role: str,
    criteria: Sequence[AcceptanceCriterion] = (),
    resolve: ArtifactResolver | None = None,
    resolve_blob: BlobResolver | None = None,
    tree: tuple[str, Mapping[str, str]] | None = None,
) -> tuple[JudgmentResult, JournalEntry, bool]:
    """Issue ONE proxy-stamped review verdict for ``node_id`` and its journal note.

    Called by the quality ``reviewer``
    node. ``node_id`` is signed into the binding (via ``seq.node_id``) AND into the
    per-request nonce, so two reviewers in one run must use distinct ids (H3).
    ``criteria`` (when given) are put in front of the judge so it reviews against
    them. ``resolve`` / ``resolve_blob`` rebuild the tree under review so only gate
    verdicts on that tree can back a finding. ``tree`` is that tree already built
    (:func:`review_tree`): when the run's last review saw its whole change and left
    a snapshot of the tree it judged, and the change since fits untruncated, this
    review is sent only that change and that verdict's findings, at medium local
    thinking, and still answers for the whole tree. Returns the
    :class:`JudgmentResult`, the advisory journal entry, and whether this review
    saw its whole change (what a :class:`ReviewedTree` records as ``complete``).
    """
    # Seq.node_id MUST equal the node_id passed to judge() — the binding
    # signs seq.node_id, so judge() rejects a mismatched record (proxy L5).
    seq = rt.seq(config, node_id, "verdict:review")

    # Fail closed on a missing server-issued nonce BEFORE anything else (the most
    # fundamental trust input of a verdict); the per-request nonce is derived
    # below, once the subject + state it binds to are known.
    require_verdict_nonce(config)

    # Primary source is the config-pinned subject (the runner sets it); the
    # state fallback selects the feature_diff by KIND, newest-by-seq (C-R1-03)
    # — never insertion-order ``artifacts[-1]``.
    state_subject = _newest_feature_diff_uri(state.get("artifacts") or [])
    subject_uri = subject_artifact_uri(config, state_subject)
    # DT2 / fail-closed: a verdict needs a concrete subject to bind to; an empty
    # subject yields a coherent-but-empty binding the gate runner can't latch.
    if not subject_uri:
        raise ValueError(
            f"{node_id} has no subject to judge: neither "
            "config['configurable']['subject_artifact_uri'] nor a state "
            "artifact is present. The supervisor/gate-wiring wave (T-M1-13) "
            "must pin subject_artifact_uri on the run config for judgment nodes."
        )
    # Config-pinned `state_sha` wins (the `mock` smoke pins it); otherwise
    # (real backends) prefer the REAL integrated tree_sha the gates ran
    # against, falling back to a stable per-run label only before any gate
    # verdict carries one.
    run_against = state_sha(config, fallback=_attested_tree_sha(state) or seq.run_id)

    # Server-issued, single-use; the agent consumes but never mints it. Derived
    # per-request (bound to node/subject/state) so each verdict in a run has its
    # OWN nonce — fail-closed when the run config has no base nonce.
    nonce = server_issued_nonce(
        config, node_id=node_id, subject_uri=subject_uri, state_sha=run_against
    )

    prior = (
        _prior_review(state, resolve, resolve_blob)
        if tree is not None and resolve is not None and resolve_blob is not None
        else None
    )
    delta: str | None = None
    keep = _always_shown(rt, state)
    if prior is not None and tree is not None:
        delta, whole = _delta_text(prior[1], tree[1], keep)
        if not whole:
            prior = delta = None
    content = (
        f"Review the artifact under {subject_uri} against the spec, the contracts and "
        "the acceptance criteria. Emit your verdict."
    )
    spec = _spec_text(rt, state)
    if spec:
        content += "\n\nThe spec:\n" + spec
    contracts = _contract_text(rt, state)
    if contracts:
        content += "\n\nThe planned contracts:\n" + contracts
    if criteria:
        content += "\n\nAcceptance criteria:\n" + "\n".join(
            f"- {c.id} [{c.criticality}]: {c.text}" for c in criteria
        )
    rules = _plan_rules_text(rt, state)
    if rules:
        content += (
            "\n\nThe plan's pinned constraints and must-haves (no check verifies these: "
            "check each one, and a violation you reproduce is a blocking finding):\n" + rules
        )
    content += (
        "\n\nThe launch gate (the factory started the real app and probed it): "
        + _launch_text(state)
    )
    if prior is None or delta is None:
        diff, complete = _diff_text(rt, state, keep)

        def body(change: str) -> str:
            return "\n\nThe change (unified diffs, applied in order):\n" + change

    else:
        verdict, complete, diff = prior[0], True, delta

        def body(change: str) -> str:
            return (
                "\n\nThis is a re-review: you already judged an earlier tree of this build "
                f"({verdict.status.value}), and a fix pass has changed it since. Your verdict "
                "is still for the WHOLE current tree. For each of your earlier findings "
                "below, decide from the change whether it is resolved: drop a resolved one, "
                "and repeat every unresolved one in your findings, with its evidence. Add "
                "only the new problems the change introduced; the rest of the tree is what "
                "you already reviewed.\n\nYour earlier findings:\n"
                + ("\n".join(f"- {f}" for f in verdict.findings) or "(none)")
                + "\n\nThe change since the tree you reviewed (unified diffs):\n"
                + change
            )

    # Without this the judge flags every build: the .feature files and criteria.json
    # never appear in the tickets' diffs because the gates write them.
    tail = (
        f"\n\nNot in the diffs by design: the factory writes {FEATURE_DIR}/*.feature (one "
        "per ticket, rendered from the acceptance criteria above) and criteria.json "
        "into the tree before the Test gate runs. Do not flag them as missing."
    )
    built = tree[0] if tree is not None else _reviewed_tree(state, resolve, resolve_blob)
    gates, tests = failing_checks(state, built)
    tail += (
        "\n\nChecks failing on this tree in its gate results: "
        + (", ".join((*gates, *tests)) or "none (no gate has failed on it)")
        + "."
    )

    def judge(change: str) -> JudgmentResult:
        return rt.proxy.judge(
            node_id,
            subject_uri,
            run_against,
            nonce,
            _SOP,
            [{"role": "user", "content": content + body(change) + tail}],
            seq=seq,
            tier=_REVIEW_TIER,
            gate_name=_GATE_NAME,
            failing_gates=gates,
            failing_tests=tests,
            effort=_REVIEW_EFFORT,
            max_tokens=_REVIEW_MAX_TOKENS,
        )

    try:
        result = judge(diff)
    except OutputLimitReached:
        # Once: the trimmed request differs, so the call cache cannot replay the miss.
        result, complete = judge(trimmed_diff(diff, _failing_findings(state), keep)), False

    # ADVISORY journal entry (plan [11]): it only ECHOES the proxy-stamped verdict
    # onto the SEPARATE `agent_journal` channel, which no gate/router reads. The
    # TRUST path is the signed `gates` write; `_merge_gates` reads ONLY that.
    note = journal_entry(
        node_id=node_id,
        role=role,
        seq=rt.seq(config, node_id, "journal"),
        summary=f"Reviewed {subject_uri} -> verdict {result.gate_result.status.value}.",
        assumptions=(f"Judged against state {run_against}.",),
        concerns=tuple(result.gate_result.findings),
    )
    return result, note, complete
