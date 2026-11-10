"""Cloud diagnoses, local edits (hybrid mode only).

Before a local-tier repair attempt in hybrid mode (a fix pass, or a local ticket's
retry), one structured call on the strongest cloud tier reads the failure and the
files it names and returns a short, concrete diagnosis; the local model then makes
the edits. Self-repair is bottlenecked on feedback quality, and feedback from a
stronger model beats self-feedback by a wide margin (Olausson et al., arXiv
2306.09896). The call rides ``rt.proxy`` like every other model call, so the
caps, the meter and attestation apply.

Advisory only: a failed or unavailable call leaves the attempt as it was, and
the diagnosis never touches a gate or widens a fix pass's write fence (files it
names outside ``fix_files`` are flagged as needing a reason).
``FOUNDRY_HYBRID_DIAGNOSE=0`` turns it off.
"""

from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from foundry import progress
from foundry.telemetry import count

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from foundry.agents._runtime import AgentRuntime
    from foundry.proxy.model_provider_proxy import ModelProviderProxy

__all__ = ["DIAGNOSE_ENV", "Diagnosis", "diagnose", "diagnosis_tier"]

DIAGNOSE_ENV = "FOUNDRY_HYBRID_DIAGNOSE"
NODE_ID = "fix_diagnosis"
_FILE_CHARS = 20_000
_SUMMARY_CHARS = 30_000
_MAX_EDITS = 6
_MAX_DO_NOT = 3
_ADVISORY = (
    "Advisory: a stronger model read the failures and the files and suggests this. "
    "It is not a verdict: check it against the failure output and `verify`, and "
    "drop any part the evidence contradicts."
)
_OUTSIDE = (
    "Named by the diagnosis but outside `fix_files`: change one only if this "
    "failure needs it, and give the reason."
)
# Proxies (one per run) that already logged a failed diagnosis: one line per run.
_WARNED: weakref.WeakSet[ModelProviderProxy] = weakref.WeakSet()
_WARNED_LOCK = threading.Lock()

_SOP = (
    "You are a senior engineer diagnosing a failed build for a junior engineer who "
    "will make the edits. Read the failure output and the files below, find the root "
    "cause, and say exactly what to change. Name only files listed below or in the "
    "failure. Be concrete: each edit names the file, the function/class/symbol and "
    "the change. Do not write the code; do not restate the failure. Reply with JSON "
    "only, matching the schema."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["root_cause", "files_to_edit", "edits", "do_not"],
    "properties": {
        "root_cause": {"type": "string", "description": "at most 3 sentences"},
        "files_to_edit": {"type": "array", "items": {"type": "string"}},
        "edits": {
            "type": "array",
            "maxItems": _MAX_EDITS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "symbol", "change"],
                "properties": {
                    "file": {"type": "string"},
                    "symbol": {"type": "string"},
                    "change": {"type": "string"},
                },
            },
        },
        "do_not": {"type": "array", "maxItems": _MAX_DO_NOT, "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class Diagnosis:
    root_cause: str
    files_to_edit: tuple[str, ...]
    edits: tuple[tuple[str, str], ...]  # (file, rendered edit)
    do_not: tuple[str, ...]

    def packet_lines(self) -> tuple[str, ...]:
        """The head of the local attempt's failure context."""
        return (
            f"DIAGNOSIS FROM A REVIEWER ({_ADVISORY})",
            f"root cause: {self.root_cause}",
            *((f"files to edit: {', '.join(self.files_to_edit)}",) if self.files_to_edit else ()),
            *(f"edit {i}: {e}" for i, (_, e) in enumerate(self.edits, 1)),
            *(f"do not: {d}" for d in self.do_not),
            "[end of diagnosis]",
        )

    def packet_field(self, fence: Collection[str]) -> dict[str, Any]:
        """A fix packet's ``diagnosis``. With a fence (``fix_files``), the files it
        names outside it are split off as needing a reason, never added to it."""
        named = dict.fromkeys((*self.files_to_edit, *(f for f, _ in self.edits)))
        outside = [f for f in named if fence and f not in fence]
        out: dict[str, Any] = {
            "advisory": _ADVISORY,
            "cause": self.root_cause,
            "files": [f for f in self.files_to_edit if f not in outside],
            "change": [e for _, e in self.edits],
        }
        if self.do_not:
            out["do_not"] = list(self.do_not)
        if outside:
            out["outside_fix_files"] = {"files": outside, "note": _OUTSIDE}
        return out

    def summary(self) -> str:
        return (
            f"{self.root_cause[:300]} | files: {', '.join(self.files_to_edit) or '-'}"
            f" | {len(self.edits)} edit(s)"
        )


def _enabled() -> bool:
    return os.environ.get(DIAGNOSE_ENV, "").strip().lower() not in ("0", "off", "false", "no")


def diagnosis_tier(rt: AgentRuntime, tier: str | None) -> str | None:
    """The cloud tier to diagnose on before an attempt on ``tier``, or None: only in
    hybrid (the adapter names a cloud escalation tier), only for a local attempt,
    and not when ``FOUNDRY_HYBRID_DIAGNOSE=0``."""
    target = getattr(rt.adapter, "escalation_tier", None)
    is_local = getattr(rt.adapter, "is_local", None)
    if not target or not callable(is_local) or not is_local(tier) or not _enabled():
        return None
    return str(target)


def _files_block(
    files: Sequence[str], tree: Mapping[str, str | None], summary: Sequence[str]
) -> list[str]:
    """The named files' content within :data:`_FILE_CHARS`; a file the summary
    already inlines is not repeated, and the last one that fits is cut short."""
    out: list[str] = []
    room = _FILE_CHARS
    for rel in dict.fromkeys(files):
        content = tree.get(rel)
        if content is None or room <= 0:
            continue
        head = f"current content of {rel}:\n"
        if any(line.startswith(head) for line in summary):
            continue
        cut = content[:room]
        room -= len(cut)
        out.append(head + cut + ("\n[... cut]" if len(cut) < len(content) else ""))
    return out


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _parse(
    raw: Mapping[str, Any], tree: Mapping[str, str | None], allowed: Collection[str]
) -> Diagnosis | None:
    """Keep what names a real path: one in the tree or in ``allowed`` (a ticket's
    own files, which may not exist yet). Anything else is dropped."""

    def real(path: Any) -> str | None:
        p = str(path or "").strip().removeprefix("./")
        return p if p and (tree.get(p) is not None or p in allowed) else None

    files = tuple(
        dict.fromkeys(p for f in raw.get("files_to_edit") or () if (p := real(f)) is not None)
    )
    edits: list[tuple[str, str]] = []
    for e in raw.get("edits") or ():
        if not isinstance(e, dict) or (path := real(e.get("file"))) is None:
            continue
        change = _clean(e.get("change"), 500)
        if change:
            symbol = _clean(e.get("symbol"), 120)
            edits.append((path, path + (f" `{symbol}`" if symbol else "") + f": {change}"))
    root = _clean(raw.get("root_cause"), 800)
    if not root and not edits:
        return None
    return Diagnosis(
        root_cause=root or "(not stated)",
        files_to_edit=files,
        edits=tuple(edits[:_MAX_EDITS]),
        do_not=tuple(d for x in (raw.get("do_not") or ())[:_MAX_DO_NOT] if (d := _clean(x, 300))),
    )


def diagnose(
    rt: AgentRuntime,
    tier: str,
    *,
    label: str,
    summary: Sequence[str],
    files: Sequence[str],
    tree: Mapping[str, str | None],
    allowed: Collection[str] = (),
) -> Diagnosis | None:
    """One structured cloud call on ``tier``; None when it fails or comes back with
    nothing usable (logged once per run), so the attempt proceeds without it.
    ``label`` names the attempt in the prompt, so two passes over an unchanged
    failure are distinct requests to the run's call cache."""
    text = "\n".join(summary)[:_SUMMARY_CHARS]
    content = "\n\n".join(
        [f"ATTEMPT: {label}", "FAILURE SUMMARY:", text, *_files_block(files, tree, summary)]
    )
    try:
        completion = rt.proxy.complete(
            NODE_ID,
            tier,
            _SOP,
            [{"role": "user", "content": content}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        got = _parse(completion.parsed or {}, tree, allowed)
    except Exception as exc:  # advisory: a spent cap is re-hit by the next cloud call
        _failed(rt, label, f"unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    if got is None:
        _failed(rt, label, "came back empty")
        return None
    count("fix.diagnosis.used")
    progress.log(f"diagnosis for {label} ({tier}): {got.summary()}")
    return got


def _failed(rt: AgentRuntime, label: str, why: str) -> None:
    count("fix.diagnosis.failed")
    with _WARNED_LOCK:
        if rt.proxy in _WARNED:
            return
        _WARNED.add(rt.proxy)
    progress.log(
        f"diagnosis for {label} {why}; proceeding without it "
        "(later failures this run are counted, not logged)"
    )
