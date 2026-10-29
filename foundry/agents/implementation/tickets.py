"""Ticket-driven scheduling (plan §2 / §2b): tickets -> tasks -> waves.

The planner emits one ``ticket`` artifact per atomic, Jira-style unit of work.
This module turns them into :class:`Task` records (deterministically — the task
id IS the ticket id, so it is stable across re-plans of the same ticket) and runs
them wave by wave from the implementation subgraph's looping ``engineering``
node:

* A **wave** is every ticket whose dependencies all have a FeatureDiff, minus
  write-scope collisions: two tickets whose ``files`` overlap (the same path, a
  ``dir/`` entry and a path under it, or an empty — unrestricted — scope) never run
  in one wave — the later one (by Seq) waits for the next wave and builds on the first.
* Each wave runs through :class:`LocalWorkPool` with ``lineage = specialty`` and a
  per-specialty slot limit under the global ``max_slots``.
* Each ticket's sandbox is seeded with the FULL integrated tree as it stands after
  the previous waves (the same ``materialize_workdir`` the gates and the fixer use),
  so the agent can read everything built so far. It may only WRITE its ``files``:
  the diff is cut back to them in-worker and a triage note names what was dropped.
* The packet carries the ticket, its scenarios, and the bodies of the interface
  contracts it implements / consumes, verbatim, after the pinned constraints.

The node returns after each wave, so the graph checkpoints per wave and a resume
(token cap, crash) skips every ticket that already has a FeatureDiff.

A ticket that fails never costs its wave-mates their work: every successful
sibling's diff is still returned. An ordinary failure is noted and retried next
pass, up to :data:`MAX_TICKET_ATTEMPTS`, then skipped so the loop ends and the gates
decide. A spent token cap is noted too (:func:`cap_stopped`); the subgraph then
exits and the supervisor raises it, after this wave's writes are committed.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.agents.implementation.junior_engineer import (
    _adapter_name,
    _apply_overlay,
    _finalize_diff,
    _mirror_diff_blob,
    _prior_failures,
    _sandbox_for,
    pinned_texts,
    ready_tasks,
)
from foundry.agents.implementation.personas import persona_for
from foundry.agents.implementation.senior_engineer import NODE_ID as SENIOR_NODE_ID
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.work_pool import (
    BudgetExceededError,
    ExecutionTask,
    LocalWorkPool,
    QuotaPolicy,
    TaskEventHook,
    allocate_lineage_quotas,
)
from foundry.proxy.token_meter import TokenCapExceeded
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    DEFAULT_TICKET_TIERS,
    FEATURE_DIR,
    Difficulty,
    FeatureDiff,
    Specialty,
    StackDecision,
    Task,
    Ticket,
    feature_path,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.execution.sandbox import Sandbox
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.architecture import PinnedConstraint
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "CONTRACT_DUP_TRIAGE",
    "CONTRACT_KIND",
    "DEFAULT_WORKERS",
    "NODE_ID",
    "TICKET_KIND",
    "cap_stopped",
    "contract_duplicates",
    "has_tickets",
    "in_write_scope",
    "make_engineering_node",
    "next_wave",
    "pending_wave",
    "plan_wave_count",
    "read_tickets",
    "task_from_ticket",
    "task_id_for_ticket",
    "tickets_settled",
    "tier_for",
    "trim_to_write_scope",
    "verify_commands",
    "verify_rules",
]

NODE_ID = "engineering"
TICKET_KIND = "ticket"
CONTRACT_KIND = "interface_contract"
_CRITERION_KIND = "acceptance_criterion"
_FEATURE_DIFF_KIND = "feature_diff"
# Triage names this module writes; SKIP doubles as the "never built" marker.
SKIP_TRIAGE = "ticket-skipped"
SCOPE_TRIAGE = "write-scope"
# One note per failed attempt (the triage union keys on name, so the attempt is in it).
FAIL_TRIAGE = "ticket-failed"
CAP_TRIAGE = "token-cap"
OVERLAY_TRIAGE = "wave-overlay"
# A ticket declared a type the contracts file already defines (a parallel copy).
CONTRACT_DUP_TRIAGE = "contract-duplicate"
# Attempts a ticket gets before it is skipped; bounds the loop on a ticket that
# keeps erroring.
MAX_TICKET_ATTEMPTS = 2

# Per-specialty concurrent ticket limit when none is configured.
DEFAULT_WORKERS: dict[str, int] = {s.value: 2 for s in Specialty}

# Claude Code needs several turns to read, write and verify; 8 left contract stubs empty.
_DEFAULT_TURN_BUDGET = 25
_DEFAULT_COST_BUDGET_MICROUSD = 1_000_000


# ---- tickets -> tasks ---------------------------------------------------------


def has_tickets(state: FactoryState) -> bool:
    """True when the planner emitted tickets (else the ownership-unit path runs)."""
    return any(a.kind == TICKET_KIND for a in (state.get("artifacts") or ()))


def read_tickets(state: FactoryState, resolve: ArtifactResolver) -> tuple[Ticket, ...]:
    """The live tickets (newest by Seq per id), in Seq order — the scheduling order.
    A newest record whose ``supersedes`` names its own id is a tombstone: a re-plan
    dropped that ticket."""
    newest: dict[str, Ticket] = {}
    for handle in (a for a in (state.get("artifacts") or ()) if a.kind == TICKET_KIND):
        body = resolve(handle)
        if not isinstance(body, Ticket):
            continue
        prior = newest.get(body.id)
        if prior is None or body.seq > prior.seq:
            newest[body.id] = body
    live = (t for t in newest.values() if t.supersedes != t.id)
    return tuple(sorted(live, key=lambda t: (t.seq, t.id)))


def task_id_for_ticket(ticket_id: str) -> str:
    """A ticket's task id is its own id: stable across re-plans of the same ticket."""
    return ticket_id


def tier_for(difficulty: Difficulty | str, ticket_tiers: Mapping[Any, str] | None) -> str:
    """Model tier for a difficulty: the run's table, else :data:`DEFAULT_TICKET_TIERS`."""
    d = Difficulty(difficulty)
    table = {Difficulty(k): v for k, v in (ticket_tiers or {}).items()}
    return table.get(d) or DEFAULT_TICKET_TIERS[d]


def task_from_ticket(
    ticket: Ticket,
    seq: Seq,
    *,
    ticket_tiers: Mapping[Any, str] | None = None,
    known: Iterable[str] = (),
) -> Task:
    """The deterministic Task for ``ticket``. Dependencies on tickets outside
    ``known`` are dropped (the planner validates the DAG; this only guards)."""
    tid = task_id_for_ticket(ticket.id)
    known_ids = set(known)
    return Task(
        id=tid,
        spec_uri=f"cas://task/{content_digest(ticket.model_dump(mode='json', exclude={'seq'}))}",
        criterion_ids=ticket.scenario_ids,
        branch=f"task/{tid}",
        ticket_id=ticket.id,
        specialty=ticket.specialty,
        tier=tier_for(ticket.difficulty, ticket_tiers),
        depends_on=tuple(task_id_for_ticket(d) for d in ticket.depends_on if d in known_ids),
        seq=seq,
    )


def _built_task_ids(state: FactoryState) -> set[str]:
    return {
        a.task_id
        for a in (state.get("artifacts") or ())
        if a.kind == _FEATURE_DIFF_KIND and a.task_id
    }


def _skipped_task_ids(state: FactoryState) -> set[str]:
    return {
        t.run_against.removeprefix("task/")
        for t in (state.get("gate_triage") or ())
        if t.name == SKIP_TRIAGE and t.produced_by == NODE_ID
    }


def _ticket_notes(state: FactoryState, prefix: str) -> dict[str, list[GateTriage]]:
    out: dict[str, list[GateTriage]] = {}
    for t in state.get("gate_triage") or ():
        if t.produced_by == NODE_ID and t.name.startswith(prefix):
            out.setdefault(t.run_against.removeprefix("task/"), []).append(t)
    return out


def cap_stopped(state: FactoryState) -> tuple[GateTriage, ...]:
    """The token-cap notes of tickets still unbuilt and unskipped: non-empty means the
    last pass stopped on a spent cap and the run must stop. A resumed pass that builds
    those tickets clears it."""
    settled = _built_task_ids(state) | _skipped_task_ids(state)
    return tuple(
        note for tid, notes in sorted(_ticket_notes(state, CAP_TRIAGE).items())
        if tid not in settled for note in notes
    )


def next_wave(
    tickets: Sequence[Ticket], done: Iterable[str], skipped: Iterable[str] = ()
) -> tuple[Ticket, ...]:
    """The tickets to build next: not done/skipped, every known dependency done, and
    no write-scope overlap with an earlier (by order) ticket of the same wave. An
    empty ``files`` (unrestricted scope) overlaps everything, so it runs alone."""
    known = {t.id for t in tickets}
    done, skipped = set(done), set(skipped)
    wave: list[Ticket] = []
    for t in tickets:
        tid = task_id_for_ticket(t.id)
        if tid in done or tid in skipped:
            continue
        if any(task_id_for_ticket(d) not in done for d in t.depends_on if d in known):
            continue
        if any(_scopes_overlap(w.files, t.files) for w in wave):
            continue
        wave.append(t)
    return tuple(wave)


def _scopes_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    if not a or not b:
        return True
    return any(
        x == y or (x.endswith("/") and y.startswith(x)) or (y.endswith("/") and x.startswith(y))
        for x in a for y in b
    )


def pending_wave(state: FactoryState, resolve: ArtifactResolver) -> tuple[Ticket, ...]:
    """The wave the engineering node would build now (empty ⇒ nothing left to run)."""
    return next_wave(read_tickets(state, resolve), _built_task_ids(state), _skipped_task_ids(state))


def tickets_settled(state: FactoryState, resolve: ArtifactResolver) -> bool:
    """No ticket is left to build (the rest are built, skipped, or blocked behind a
    skipped one) and at least one was built — the fix loop may repair the
    integrated tree instead of building tickets."""
    return bool(_built_task_ids(state)) and not pending_wave(state, resolve)


def plan_wave_count(tickets: Sequence[Ticket]) -> int:
    """How many waves the plan takes when every ticket builds."""
    done: set[str] = set()
    waves = 0
    while wave := next_wave(tickets, done):
        done.update(task_id_for_ticket(t.id) for t in wave)
        waves += 1
    return waves


# ---- write scope --------------------------------------------------------------


def _is_gate_input(path: str) -> bool:
    from foundry.graph.gate_wiring import GATE_INPUT_FILES

    return path in GATE_INPUT_FILES or (
        path.startswith(f"{FEATURE_DIR}/") and path.endswith(".feature")
    )


# A manifest's lockfiles: written by the package manager whenever the manifest's
# owner installs, so they belong to that ticket's scope (dropping one leaves a
# lockfile-strict install like `npm ci` nothing to install from).
_LOCKFILES: dict[str, tuple[str, ...]] = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
                     "pnpm-lock.yaml"),
    "Cargo.toml": ("Cargo.lock",),
    "go.mod": ("go.sum",),
    "pyproject.toml": ("uv.lock", "poetry.lock"),
}


def in_write_scope(path: str, files: Sequence[str]) -> bool:
    """May a ticket with write scope ``files`` write ``path``? Gate inputs (criteria,
    feature files) never; an entry ending in ``/`` admits its subtree, a package
    manifest its lockfiles; an empty scope admits everything else (the ticket
    declared none)."""
    if _is_gate_input(path):
        return False
    if not files:
        return True
    return any(
        path == f or (f.endswith("/") and path.startswith(f)) or _is_lockfile_of(path, f)
        for f in files
    )


def _is_lockfile_of(path: str, manifest: str) -> bool:
    m_dir, _, m_name = manifest.rpartition("/")
    p_dir, _, p_name = path.rpartition("/")
    return m_dir == p_dir and p_name in _LOCKFILES.get(m_name, ())


def trim_to_write_scope(
    result: AdapterResult, sandbox: Sandbox, files: Sequence[str], task_id: str
) -> AdapterResult:
    """Cut the adapter's diff back to ``files``; record the dropped paths as triage.

    Called in-worker while the sandbox is alive. An unparseable body is returned
    as-is (integration fails it closed). Modification hunks are applied against the
    sandbox's base file so the trimmed body is a self-contained full-file snapshot."""
    from foundry.graph.gate_wiring import MaterializationError, _apply_hunks, _diff_ops

    probe = FeatureDiff(
        diff_uri=result.diff_uri, task_id=task_id, branch=f"task/{task_id}",
        base_tree_sha="", seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        ops = _diff_ops(probe, sandbox.get_blob)
    except MaterializationError:
        return result
    touched = set(ops.writes) | set(ops.deletes) | set(ops.patches)
    dropped = sorted(p for p in touched if not in_write_scope(p, files))
    if not dropped:
        return result
    kept: dict[str, str] = {p: c for p, c in ops.writes.items() if in_write_scope(p, files)}
    worktree = Path(sandbox.worktree)
    for rel, hunks in ops.patches.items():
        if not in_write_scope(rel, files):
            continue
        base = worktree / rel
        original = base.read_text(encoding="utf-8") if base.is_file() else ""
        try:
            kept[rel] = _apply_hunks(original, hunks, diff_uri=result.diff_uri, rel=rel)
        except MaterializationError:
            dropped.append(rel)
    payload: dict[str, Any] = {"kind": "full-file-snapshot", "files": kept}
    deleted = [p for p in ops.deletes if in_write_scope(p, files)]
    if deleted:
        payload["deleted"] = deleted
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    note = GateTriage(
        name=SCOPE_TRIAGE,
        run_against=f"task/{task_id}",
        explanation="dropped writes outside the ticket's files: " + ", ".join(sorted(dropped)),
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(
        update={"diff_uri": sandbox.put_blob(blob), "triage": (*result.triage, note)}
    )


class _ScopedAdapter:
    """``rt.adapter`` + in-worker write-scope trim + diff-blob mirror into ``rt.cas``
    (both need the sandbox, which the pool destroys right after ``run``)."""

    def __init__(
        self,
        rt: AgentRuntime,
        scopes: Mapping[str, tuple[str, ...]],
        contracts: Mapping[str, str] | None = None,
    ) -> None:
        assert rt.adapter is not None
        self._rt = rt
        self._scopes = scopes
        self._contracts = dict(contracts or {})
        self.supports_turn_snapshots = rt.adapter.supports_turn_snapshots

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task.get("id"))
        result = self._rt.adapter.run(packet, sandbox)  # type: ignore[union-attr]
        files = self._scopes.get(tid, ())
        result = trim_to_write_scope(result, sandbox, files, tid)
        _require_scope_written(result, sandbox, files, tid)
        result = _flag_contract_duplicates(result, sandbox, self._contracts, tid)
        _mirror_diff_blob(self._rt, sandbox, result)
        return result


class EmptyTicketError(RuntimeError):
    """A ticket's session ended without any of its ``files`` in the tree."""


def _require_scope_written(
    result: AdapterResult, sandbox: Sandbox, files: Sequence[str], task_id: str
) -> None:
    """Fail a ticket whose diff touches none of its ``files`` while none of them
    exists yet: recording that empty diff as built would unblock its dependents on
    a tree without the work (a session that ran out of turns before writing). It
    becomes an ordinary failed attempt — retried, then skipped. An unrestricted
    scope or an unparseable body is left to integration."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops

    if not files:
        return
    probe = FeatureDiff(
        diff_uri=result.diff_uri, task_id=task_id, branch=f"task/{task_id}",
        base_tree_sha="", seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        ops = _diff_ops(probe, sandbox.get_blob)
    except MaterializationError:
        return
    if set(ops.writes) | set(ops.deletes) | set(ops.patches):
        return
    worktree = Path(sandbox.worktree)
    if any((worktree / f.rstrip("/")).exists() for f in files):
        return
    raise EmptyTicketError(
        f"the session wrote none of the ticket's files ({', '.join(files)})"
    )


# A top-level (column-0) type declaration in the common languages: Python/TS/JS
# classes, TS interfaces and type aliases, Go types, Rust structs/enums/traits,
# Java/Kotlin classes, interfaces and records.
_TYPE_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:public\s+)?(?:(?:abstract|final|sealed|data)\s+)*"
    r"(?:class|interface|type|struct|enum|trait|record)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_SOURCE_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs",
                    ".cjs", ".go", ".rs", ".java", ".kt", ".cs", ".swift")


def _declared_types(text: str) -> set[str]:
    return set(_TYPE_DECL.findall(text))


def contract_duplicates(
    files: Mapping[str, str | None], contract_files: Sequence[str]
) -> tuple[str, ...]:
    """One finding per source file outside ``contract_files`` that declares a
    top-level type with the name of a type the contract files declare: a parallel
    copy (``models.Category`` beside ``contracts.Category``) type-checks on its own
    and then fails to fit every caller that uses the contract's type."""
    names: set[str] = set()
    for f in contract_files:
        names |= _declared_types(files.get(f) or "")
    if not names:
        return ()
    out: list[str] = []
    for path, text in sorted(files.items()):
        if path in contract_files or not text or not path.endswith(_SOURCE_SUFFIXES):
            continue
        dup = sorted(_declared_types(text) & names)
        if dup:
            out.append(
                f"{path} redeclares contract type(s) {', '.join(dup)} already defined in "
                f"{', '.join(contract_files)}: delete the copy and import the contract's "
                "type instead"
            )
    return tuple(out)


def _written_sources(
    result: AdapterResult, sandbox: Sandbox, task_id: str
) -> dict[str, str | None]:
    """The final text of every file the session's diff writes (a patched file is
    read off the still-live worktree); empty when the body cannot be parsed."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops

    probe = FeatureDiff(
        diff_uri=result.diff_uri, task_id=task_id, branch=f"task/{task_id}",
        base_tree_sha="", seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        ops = _diff_ops(probe, sandbox.get_blob)
    except MaterializationError:
        return {}
    out: dict[str, str | None] = dict(ops.writes)
    for rel in sorted(set(ops.patches) - set(ops.writes)):
        path = Path(sandbox.worktree) / rel
        try:
            out[rel] = path.read_text(encoding="utf-8") if path.is_file() else None
        except UnicodeDecodeError:
            out[rel] = None
    return out


def _flag_contract_duplicates(
    result: AdapterResult, sandbox: Sandbox, contracts: Mapping[str, str], task_id: str
) -> AdapterResult:
    if not contracts or task_id == task_id_for_ticket(CONTRACTS_TICKET_ID):
        return result
    written = _written_sources(result, sandbox, task_id)
    findings = contract_duplicates({**written, **contracts}, tuple(contracts))
    if not findings:
        return result
    note = GateTriage(
        name=CONTRACT_DUP_TRIAGE,
        run_against=f"task/{task_id}",
        explanation="; ".join(findings)[:2000],
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(update={"triage": (*result.triage, note)})


# ---- self-verification ------------------------------------------------------

_VERIFY_STEPS = ("install", "build", "typecheck", "test", "bdd")


def verify_commands(stack: StackDecision | None) -> dict[str, str]:
    """The stack's planned commands an agent runs on its own work (the gates run
    the same ones on the integrated tree)."""
    if stack is None:
        return {}
    return {k: v for k in _VERIFY_STEPS if (v := getattr(stack, k))}


# Type-check ONE file in isolation, for the contracts ticket (its siblings in wave
# 0 may not have written the project config yet).
_SINGLE_FILE_TYPECHECK: dict[str, str] = {
    "python": "python -m mypy {file}",
    "typescript": "npx tsc --noEmit --strict --skipLibCheck {file}",
}


def verify_rules(
    stack: StackDecision | None,
    *,
    ticket_id: str = "",
    contract_files: Sequence[str] = (),
) -> list[str]:
    """The packet rules that make an agent check its own work before it finishes:
    run the stack's commands (``verify``) and fix what fails in its own files; the
    contracts ticket writes declarations only and type-checks its file alone; every
    other ticket uses the contract types instead of re-declaring them."""
    cmds = verify_commands(stack)
    rules = [
        "VERIFY BEFORE YOU FINISH: from this directory run "
        + ("the commands in `verify` (install only once its package manifest exists; "
           "then build and typecheck; then the tests that cover your files)"
           if cmds else "the project's build, type checker and the tests that cover "
           "your files")
        + ". Fix every failure in the files you may write and re-run until they pass. "
        "Failures in other files, or caused by files later tickets have not written "
        "yet, are expected: leave them and name them in a TRIAGE line. If a command "
        "cannot run here (missing tool, no network), say so in a TRIAGE line."
    ]
    if "bdd" in cmds and "FOUNDRY_BDD_REPORT" in cmds["bdd"]:
        rules.append("The bdd command writes its report to $FOUNDRY_BDD_REPORT: set it "
                     "to a scratch file outside this directory when you run it.")
    if ticket_id == task_id_for_ticket(CONTRACTS_TICKET_ID):
        files = ", ".join(contract_files) or "the contracts file"
        language = stack.language if stack else ""
        single = _SINGLE_FILE_TYPECHECK.get(language, "")
        check = (single.format(file=" ".join(contract_files)) if single and contract_files
                 else cmds.get("typecheck", "the language's type checker"))
        rules.append(
            f"DECLARATIONS ONLY in {files}: types, interfaces and signatures with no "
            "logic — Python: Protocol, dataclass, TypedDict and Enum classes whose "
            "methods have `...` bodies; TypeScript: interface and type declarations "
            "(plus the enum/const values the contracts name). No implementations, "
            "helpers, I/O or validation. Type-check this file on its own before you "
            f"finish (`{check}`) and fix every error it reports."
        )
    elif contract_files:
        rules.append(
            "CONTRACT TYPES ARE THE ONLY TYPES: the data types, interfaces and errors "
            f"in {', '.join(contract_files)} are the single definition of each. Import "
            "them from there; never declare a class, interface or type with the same "
            "name in another file (no parallel models.X copy of a contract type) — a "
            "copy type-checks alone and breaks every caller that uses the contract's "
            "type."
        )
    return rules


# ---- context ------------------------------------------------------------------


def _integrated_overlay(
    state: FactoryState, resolve: ArtifactResolver, resolve_blob: BlobResolver
) -> tuple[dict[str, str | None], str | None]:
    """The integrated tree after the previous waves (feature files included, other
    gate inputs excluded), as an overlay for each ticket's fresh sandbox — plus the
    materialization error when the tree could not be built (overlay then empty)."""
    from foundry.graph.gate_wiring import (
        GATE_INPUT_FILES,
        MaterializationError,
        materialize_workdir,
    )

    with tempfile.TemporaryDirectory(prefix="foundry-wave-") as tmp:
        root = Path(tmp)
        try:
            materialize_workdir(state, root, resolve=resolve, resolve_blob=resolve_blob)
        except MaterializationError as exc:
            return {}, str(exc)
        out: dict[str, str | None] = {}
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root).as_posix()
            if not f.is_file() or rel in GATE_INPUT_FILES:
                continue
            try:
                out[rel] = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return out, None


def _newest_by(
    state: FactoryState, resolve: ArtifactResolver, kind: str, typ: type[Any], key: str
) -> dict[str, Any]:
    newest: dict[str, tuple[Seq, Any]] = {}
    for handle in (a for a in (state.get("artifacts") or ()) if a.kind == kind):
        body = resolve(handle)
        if isinstance(body, typ):
            k = getattr(body, key)
            if k not in newest or handle.seq > newest[k][0]:
                newest[k] = (handle.seq, body)
    return {k: v[1] for k, v in newest.items()}


def _contract_body(rt: AgentRuntime, contract: InterfaceContract) -> str:
    """The contract's authored body (persisted prose), else its record as JSON."""
    if rt.cas is not None:
        from foundry.schemas.docs import ProseBody

        try:
            body = rt.cas.get_body(contract.spec_uri)
        except Exception:
            body = None
        if isinstance(body, ProseBody):
            return body.text
    return json.dumps(
        {"name": contract.name, "spec_uri": contract.spec_uri,
         "consumers": list(contract.consumers)},
        sort_keys=True, ensure_ascii=False,
    )


def _scenario_text(c: AcceptanceCriterion, owner: str) -> str:
    head = f"SCENARIO {c.id} [{scenario_ref(owner, c.id)}]"
    if not (c.given or c.when or c.then):
        return f"{head}: {c.text}"
    lines = [head + ":"]
    for word, steps in (("Given", c.given), ("When", c.when), ("Then", c.then)):
        for i, step in enumerate(steps):
            lines.append(f"  {word if i == 0 else 'And'} {step}")
    return "\n".join(lines)


def _ticket_context(
    rt: AgentRuntime,
    ticket: Ticket,
    criteria: Mapping[str, AcceptanceCriterion],
    contracts: Mapping[str, InterfaceContract],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(spans, feature files): the ticket's scenarios and its contracts, verbatim."""
    spans: list[str] = []
    features: set[str] = set()
    for cid in ticket.scenario_ids:
        c = criteria.get(cid)
        if c is None:
            continue
        owner = c.ticket_id or ticket.id
        features.add(feature_path(owner))
        spans.append(_scenario_text(c, owner))
    for role, names in (
        ("this ticket IMPLEMENTS it — match it exactly", ticket.implements),
        ("this ticket CALLS it — code against it exactly", ticket.consumes),
    ):
        for name in names:
            contract = contracts.get(name)
            body = _contract_body(rt, contract) if contract else "(not in the plan)"
            spans.append(f"INTERFACE CONTRACT {name} ({role}):\n{body}")
    return tuple(spans), tuple(sorted(features))


def _ticket_packet(
    task: Task,
    ticket: Ticket,
    constraints: Sequence[PinnedConstraint],
    *,
    spans: tuple[str, ...],
    feature_files: tuple[str, ...],
    prior_failures: tuple[str, ...],
    turn_budget: int,
    cost_budget_microusd: int,
    stack: StackDecision | None = None,
    contract_files: Sequence[str] = (),
) -> TaskPacket:
    rules = [
        "You can read the whole project in the working directory: it holds everything "
        "the earlier tickets built.",
        f"Never edit {FEATURE_DIR}/*.feature files: they are the executable spec.",
        *verify_rules(stack, ticket_id=task.id, contract_files=contract_files),
    ]
    if ticket.files:
        rules.insert(1, "Write ONLY these paths; changes anywhere else are discarded: "
                     + ", ".join(ticket.files) + ".")
    return TaskPacket(
        task={
            "id": task.id,
            "ticket_id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "specialty": ticket.specialty.value,
            "difficulty": ticket.difficulty.value,
            "tier": task.tier,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
            "depends_on": list(task.depends_on),
            "files": list(ticket.files),
            "reads": list(ticket.reads),
            "feature_files": list(feature_files),
            "rules": rules,
            "verify": verify_commands(stack),
        },
        persona=persona_for(ticket.specialty.value, stack),
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=spans,
        prior_failures=prior_failures,
        worktree_path=task.branch,  # placeholder; adapters use sandbox.worktree
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )


def _failure_notes(
    rt: AgentRuntime, config: RunnableConfig, tid: str, attempt: int, exc: Exception
) -> list[GateTriage]:
    """The triage for a ticket's ``attempt``-th failure, plus the skip marker once
    :data:`MAX_TICKET_ATTEMPTS` is spent (so the wave loop terminates)."""
    notes = [GateTriage(
        name=f"{FAIL_TRIAGE}:{attempt}", run_against=f"task/{tid}",
        explanation=f"attempt {attempt} failed: {exc}"[:2000], produced_by=NODE_ID,
        seq=rt.seq(config, NODE_ID, f"fail:{tid}"),
    )]
    if attempt >= MAX_TICKET_ATTEMPTS:
        notes.append(GateTriage(
            name=SKIP_TRIAGE, run_against=f"task/{tid}",
            explanation=f"not built: failed {attempt} attempt(s); last error: {exc}"[:2000],
            produced_by=NODE_ID, seq=rt.seq(config, NODE_ID, f"skip:{tid}"),
        ))
    return notes


def _order_after_integrated(state: FactoryState, merged: dict[str, list[Any]]) -> None:
    """Sort this wave's diffs after every diff already integrated.

    Integration applies diffs in Seq order, but a ticket diff's own Seq does not
    follow build order: the fixer lifts its epoch past every diff it repairs (a
    per-wave fix lands BEFORE the next wave), and a resumed pass restarts the
    superstep index. A later wave builds on the integrated tree and may rewrite a
    file an earlier diff wrote, so its diff must apply last or its edit is lost."""
    top = max(
        (a.seq.emission_epoch for a in (state.get("artifacts") or ())
         if a.kind == _FEATURE_DIFF_KIND),
        default=0,
    )
    if top and merged.get("artifacts"):
        merged["artifacts"] = [
            a.model_copy(update={"seq": a.seq._replace(emission_epoch=top + 1)})
            if a.kind == _FEATURE_DIFF_KIND and a.seq.emission_epoch <= top else a
            for a in merged["artifacts"]
        ]


def _round_robin(wave: Sequence[Ticket]) -> list[Ticket]:
    """Interleave specialties so a busy specialty's queued tickets don't hold every
    pool thread while another specialty has free slots."""
    by: dict[str, list[Ticket]] = {}
    for t in wave:
        by.setdefault(t.specialty.value, []).append(t)
    out: list[Ticket] = []
    while any(by.values()):
        for q in by.values():
            if q:
                out.append(q.pop(0))
    return out


# ---- the engineering node -----------------------------------------------------


def make_engineering_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver | None = None,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
    workers: Mapping[str, int] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
    on_task: TaskEventHook | None = None,
) -> NodeFn:
    """Build the looping ``engineering`` node: each call builds ONE wave.

    ``workers`` caps concurrent tickets per specialty (default
    :data:`DEFAULT_WORKERS`) under the global ``max_slots``; ``ticket_tiers`` maps
    difficulty -> model tier; ``on_task`` receives each ticket's start/done/failed
    :class:`TaskEvent`. ``cost_cap_microusd`` is the run's envelope, split across
    specialties by ticket count and spent down across waves."""
    if rt.adapter is None:
        raise ValueError("engineering requires rt.adapter (a WorkerAdapter)")
    limits = {**DEFAULT_WORKERS, **(workers or {})}

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        tickets = read_tickets(state, resolve)
        known = [t.id for t in tickets]
        on_channel = {t.id: t for t in ready_tasks(state)}
        tasks: dict[str, Task] = {}
        new_tasks: list[dict[str, Any]] = []
        for i, ticket in enumerate(tickets):
            tid = task_id_for_ticket(ticket.id)
            # The tasks channel keeps a task's FIRST record; a re-planned ticket's
            # tier and dependencies come from its newest version, re-derived here.
            existing = on_channel.get(tid)
            seq = (existing.seq if existing
                   else rt.seq(config, SENIOR_NODE_ID, f"task:{tid}", local=i))
            tasks[tid] = task_from_ticket(ticket, seq, ticket_tiers=ticket_tiers, known=known)
            if existing is None:
                new_tasks.append(tasks[tid].model_dump(mode="json"))
        writes: dict[str, Any] = {"tasks": new_tasks} if new_tasks else {}

        done = _built_task_ids(state)
        wave = next_wave(tickets, done, _skipped_task_ids(state))
        if not wave:
            return writes

        constraints: Sequence[PinnedConstraint] = state.get("pinned_constraints") or ()
        blobs = resolve_blob or (rt.cas.resolve_blob if rt.cas is not None else None)
        overlay: dict[str, str | None] = {}
        overlay_error: str | None = None
        if blobs is not None:
            overlay, overlay_error = _integrated_overlay(state, resolve, blobs)
        criteria = _newest_by(state, resolve, _CRITERION_KIND, AcceptanceCriterion, "id")
        contracts = _newest_by(state, resolve, CONTRACT_KIND, InterfaceContract, "name")
        prior = _prior_failures(state)
        from foundry.graph.gate_wiring import contract_paths

        contract_files = contract_paths(state, resolve)
        built_contracts = {f: c for f in contract_files if isinstance(c := overlay.get(f), str)}

        # The run's envelope, split across specialties by ticket count; spend of
        # tickets built in earlier waves stays committed so the cap spans the run.
        cap = cost_cap_microusd or 0
        weights: dict[str, int] = {}
        spent: dict[str, int] = {}
        for t in tickets:
            sp = t.specialty.value
            weights[sp] = weights.get(sp, 0) + 1
            if task_id_for_ticket(t.id) in done:
                spent[sp] = spent.get(sp, 0) + cost_budget_microusd
        quota = QuotaPolicy(
            ledger=BudgetLedger(cap_microusd=cap),
            lineage_quota_microusd=allocate_lineage_quotas(weights, cap),
            _committed_total=sum(spent.values()),
            _committed_by_lineage=spent,
        )

        bases: dict[str, str] = {}

        def make_sandbox(etask: ExecutionTask) -> Sandbox:
            sandbox = _sandbox_for(rt, source_dir, snapshot_store)
            if overlay:
                _apply_overlay(sandbox.worktree, overlay)
            bases[str(etask.packet.task["id"])] = sandbox.snapshot()
            return sandbox

        scoped = _ScopedAdapter(rt, {task_id_for_ticket(t.id): t.files for t in wave},
                                built_contracts)
        adapter_name = _adapter_name(rt)
        submitted: dict[str, str] = {}
        merged: dict[str, list[Any]] = {}
        if overlay_error is not None:
            # This wave builds on the bare base, not on the earlier waves: say so.
            merged["gate_triage"] = [GateTriage(
                name=OVERLAY_TRIAGE, run_against=f"wave/{wave[0].id}",
                explanation="the integrated tree could not be materialized, so this wave "
                f"builds on the bare base without the earlier waves' work: {overlay_error}"[:2000],
                produced_by=NODE_ID, seq=rt.seq(config, NODE_ID, f"overlay:{wave[0].id}"),
            )]
        with LocalWorkPool(
            sandbox_factory=make_sandbox,
            adapter_factory=lambda _name: scoped,
            max_slots=min(len(wave), max_slots),
            quota=quota,
            limits=limits,
            on_task=on_task,
        ) as pool:
            for ticket in _round_robin(wave):
                tid = task_id_for_ticket(ticket.id)
                spans, features = _ticket_context(rt, ticket, criteria, contracts)
                packet = _ticket_packet(
                    tasks[tid], ticket, constraints, spans=spans, feature_files=features,
                    prior_failures=prior, turn_budget=turn_budget,
                    cost_budget_microusd=cost_budget_microusd, stack=state.get("stack"),
                    contract_files=contract_files,
                )
                etask = ExecutionTask(
                    packet=packet, adapter=adapter_name, lineage=ticket.specialty.value,
                    est_cost_microusd=cost_budget_microusd,
                )
                try:
                    submitted[tid] = pool.submit(etask)
                except BudgetExceededError as exc:
                    merged.setdefault("gate_triage", []).append(GateTriage(
                        name=SKIP_TRIAGE, run_against=f"task/{tid}",
                        explanation=f"not built: {exc}", produced_by=NODE_ID,
                        seq=rt.seq(config, NODE_ID, f"skip:{tid}"),
                    ))
            # Finalize in Seq order, single-threaded: rt.seq and rt.cas body writes
            # never race. A failed ticket never takes its siblings' diffs with it.
            failures = _ticket_notes(state, FAIL_TRIAGE)
            built: list[Ticket] = []
            for ticket in wave:
                tid = task_id_for_ticket(ticket.id)
                if tid not in submitted:
                    continue
                try:
                    result = pool.result(submitted[tid])
                    out = _finalize_diff(rt, tasks[tid], constraints, bases[tid], result, config)
                except TokenCapExceeded as exc:
                    merged.setdefault("gate_triage", []).append(GateTriage(
                        name=CAP_TRIAGE, run_against=f"task/{tid}", explanation=str(exc),
                        produced_by=NODE_ID, seq=rt.seq(config, NODE_ID, f"cap:{tid}"),
                    ))
                    continue
                except Exception as exc:
                    merged.setdefault("gate_triage", []).extend(
                        _failure_notes(rt, config, tid, len(failures.get(tid, ())) + 1, exc)
                    )
                    continue
                built.append(ticket)
                for channel, records in out.items():
                    merged.setdefault(channel, []).extend(records)

        _order_after_integrated(state, merged)
        for channel, records in merged.items():
            writes.setdefault(channel, []).extend(records)
        writes.setdefault("agent_journal", []).append(journal_entry(
            node_id=NODE_ID, role="Engineering",
            seq=rt.seq(config, NODE_ID, f"journal:wave:{wave[0].id}"),
            summary=f"Built {len(built)} ticket(s) this wave: " + ", ".join(
                f"{t.id} ({t.specialty.value}, {tasks[task_id_for_ticket(t.id)].tier})"
                for t in built
            ),
            concerns=(f"Integrated tree unavailable, built on the bare base: {overlay_error}",)
            if overlay_error else (),
        ))
        return writes

    return _node
