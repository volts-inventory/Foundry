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
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.implementation import FEATURE_DIR, FeatureDiff
from foundry.schemas.quality import ReviewedTree

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


def _capped(text: str) -> tuple[str, bool]:
    """``text`` cut to :data:`_DIFF_CHAR_CAP` with a ``[TRUNCATED]`` note, and
    whether it fit whole."""
    if len(text) <= _DIFF_CHAR_CAP:
        return text, True
    note = f"\n\n[TRUNCATED: showing the first {_DIFF_CHAR_CAP} of {len(text)} chars.]"
    return text[:_DIFF_CHAR_CAP] + note, False


def _diff_text(rt: AgentRuntime, state: FactoryState) -> tuple[str, bool]:
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
    out, whole = _capped("\n\n".join(parts))
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


def _delta_text(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, bool]:
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
    return _capped("\n".join(parts))


_SOP = (
    "You are the Reviewer/Critic, the evaluator side of the evaluator-optimizer "
    "loop. Review the diff for correctness, design adherence, readability, and "
    "scrutinize the TEST diffs for weakened assertions. Return a verdict: "
    "approve only if the change is correct and the tests still bind the "
    "criteria; otherwise request-changes with specific, actionable findings. "
    "A finding blocks only if it cites real evidence: a check that is failing in "
    "this tree's gate results (listed in the request) in failing_check, or a "
    "reproducible command with an expected result and a DIFFERENT actual result "
    "you observed. Citing a passing test or scenario does not count. Opinions on "
    "test quality (fragile asserts, missing edge cases, needless mocks) are "
    "advisory while every check passes: record them, they do not block. "
    "Report at most 10 findings, most important first, each in a sentence or two; "
    "never repeat a finding."
)


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
    if prior is not None and tree is not None:
        delta, whole = _delta_text(prior[1], tree[1])
        if not whole:
            prior = delta = None
    content = (
        f"Review the artifact under {subject_uri} against the interfaces and "
        "acceptance criteria. Emit your verdict."
    )
    if criteria:
        content += "\n\nAcceptance criteria:\n" + "\n".join(
            f"- {c.id} [{c.criticality}]: {c.text}" for c in criteria
        )
    if prior is None or delta is None:
        diff, complete = _diff_text(rt, state)
        content += "\n\nThe change (unified diffs, applied in order):\n" + diff
    else:
        verdict, complete = prior[0], True
        content += (
            "\n\nThis is a re-review: you already judged an earlier tree of this build "
            f"({verdict.status.value}), and a fix pass has changed it since. Your verdict is "
            "still for the WHOLE current tree. For each of your earlier findings below, "
            "decide from the change whether it is resolved: drop a resolved one, and "
            "repeat every unresolved one in your findings, with its evidence. Add only "
            "the new problems the change introduced; the rest of the tree is what you "
            "already reviewed.\n\nYour earlier findings:\n"
            + ("\n".join(f"- {f}" for f in verdict.findings) or "(none)")
            + "\n\nThe change since the tree you reviewed (unified diffs):\n"
            + delta
        )
    # Without this the judge flags every build: the .feature files and criteria.json
    # never appear in the tickets' diffs because the gates write them.
    content += (
        f"\n\nNot in the diffs by design: the factory writes {FEATURE_DIR}/*.feature (one "
        "per ticket, rendered from the acceptance criteria above) and criteria.json "
        "into the tree before the Test gate runs. Do not flag them as missing."
    )
    built = tree[0] if tree is not None else _reviewed_tree(state, resolve, resolve_blob)
    gates, tests = failing_checks(state, built)
    content += (
        "\n\nChecks failing on this tree in its gate results: "
        + (", ".join((*gates, *tests)) or "none (no gate has failed on it)")
        + "."
    )
    result = rt.proxy.judge(
        node_id,
        subject_uri,
        run_against,
        nonce,
        _SOP,
        [{"role": "user", "content": content}],
        seq=seq,
        tier=_REVIEW_TIER,
        gate_name=_GATE_NAME,
        failing_gates=gates,
        failing_tests=tests,
        effort=None if prior is None else "medium",
    )

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
