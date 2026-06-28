"""LLM completeness critic for the platform deepening loop.

After a service passes its gates, this asks a model one question: *is this service
technically near-complete, and if not, what's missing?* The verdict drives the
deepening loop — stop when it says complete, otherwise feed the named gaps into the
next pass so the rebuild is TARGETED, not a blind rerun.

It is advisory and fail-open: on ``mock`` (keyless) it returns "complete" so builds
stay deterministic and cheap; on a real backend a parse/transport failure returns
``None`` (couldn't assess) so the loop falls back to its structural plateau signal.
It never gates the release — the deterministic gates own that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_MOCK = "mock"
_MAX_FILES = 40
_MAX_BYTES_PER_FILE = 4000
_CODE_SUFFIXES = (".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".lua", ".sql", ".rs",
                  ".java", ".rb", ".c", ".cpp", ".h", ".css", ".html", ".sh", ".toml",
                  ".yaml", ".yml", ".json", ".mod")

_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["complete", "gaps"],
}

_SOP = (
    "You are a senior staff engineer doing a COMPLETENESS review of one service. "
    "Given the service's request and its current code tree, judge whether the "
    "TECHNICAL implementation is near-complete: the core features actually implemented "
    "(not stubbed), with input validation, error handling, the obvious edge cases, and "
    "tests. Ignore business/marketing/legal scope. Return JSON: `complete` (true only "
    "if there is no MEANINGFUL technical work left), `gaps` (concrete, actionable "
    "missing pieces — empty when complete), and a one-line `summary`."
)


@dataclass(frozen=True)
class CompletenessVerdict:
    complete: bool
    gaps: tuple[str, ...] = ()
    summary: str = ""


def _read_tree(path: str) -> str:
    """A bounded snapshot of the tree's code: file list + truncated contents."""
    chunks: list[str] = []
    count = 0
    for root, dirs, files in os.walk(path):
        if "docs" in dirs:
            dirs.remove("docs")
        for f in sorted(files):
            if count >= _MAX_FILES:
                break
            if not f.endswith(_CODE_SUFFIXES):
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, path)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    body = fh.read(_MAX_BYTES_PER_FILE)
            except OSError:
                continue
            chunks.append(f"=== {rel} ===\n{body}")
            count += 1
    return "\n\n".join(chunks) or "(empty tree)"


def assess_completeness(
    request: str, tree_path: str, *, backend: str,
) -> CompletenessVerdict | None:
    """Judge whether the service at ``tree_path`` is technically near-complete.

    Returns a verdict, or ``None`` when no judgment could be made (so the caller
    falls back to its structural signal). ``mock`` short-circuits to "complete".
    """
    if backend == _MOCK:
        return CompletenessVerdict(complete=True, summary="mock backend: no model judgment")
    try:
        from foundry.cli import _build_proxy

        proxy = _build_proxy(backend)
        prompt = (
            f"SERVICE REQUEST:\n{request}\n\n"
            f"CURRENT CODE TREE (truncated):\n{_read_tree(tree_path)}\n\n"
            "Is this service technically near-complete? Reply with the JSON verdict."
        )
        res = proxy.complete(
            "completeness_critic", "T1", _SOP,
            [{"role": "user", "content": prompt}],
            purpose="judgment", schema=_SCHEMA,
        )
        v = res.parsed or {}
        if "complete" not in v:
            return None
        return CompletenessVerdict(
            complete=bool(v.get("complete")),
            gaps=tuple(str(g) for g in (v.get("gaps") or []) if str(g).strip()),
            summary=str(v.get("summary", "")),
        )
    except Exception:
        return None  # couldn't assess -> caller uses the plateau signal instead
