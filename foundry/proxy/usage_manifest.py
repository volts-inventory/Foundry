"""Per-run token accounting for the model-egress PROXY — 'where did the
orchestration spend go'.

A :class:`UsageManifest` accumulates every ``rt.proxy`` call's usage bucketed by
``(node_id, tier)`` and renders a plain-text report: per-node rows (sorted by
output tokens) + grand totals. This is the product / architecture / quality /
assurance / judgment + Senior-decomposition spend — everything that flows through
the single proxy egress.

IMPORTANT BOUNDARY: the CODING substrate (the Junior Engineer running the
``claude-code`` CLI or another WorkerAdapter) does NOT go through the proxy — the
adapter reports only ``turns_used``, and its tokens live in the substrate's own
accounting. So this manifest deliberately EXCLUDES the coder's tokens (typically
the dominant cost); it measures the orchestration layer around the coder. Capturing
the coder's tokens needs the adapter contract to surface usage (a follow-up).

Advisory ONLY: it observes the proxy's per-call usage stream (via
:func:`foundry.proxy.model_provider_proxy.register_usage_observer`) and never feeds
anything back into a build. Thread-safe.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping

_FIELDS = ("calls", "input", "output", "cache_read")

# The tier the coding substrate reports under (via emit_usage) — its own tokens,
# which never flow through the proxy. Its presence flips the manifest from
# "proxy-only" to a full coder-included accounting.
_CODE_TIER = "code"


class UsageManifest:
    """Thread-safe per-``(node_id, tier)`` token accumulator + text report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: dict.fromkeys(_FIELDS, 0)
        )

    def observe(self, node_id: str, tier: str, usage: Mapping[str, int]) -> None:
        """Record one successful call. Matches the ``register_usage_observer`` contract."""
        with self._lock:
            row = self._rows[(node_id, tier)]
            row["calls"] += 1
            row["input"] += int(usage.get("input_tokens", 0) or 0)
            row["output"] += int(usage.get("output_tokens", 0) or 0)
            row["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)

    def totals(self) -> dict[str, int]:
        with self._lock:
            out = dict.fromkeys(_FIELDS, 0)
            for row in self._rows.values():
                for k in _FIELDS:
                    out[k] += row[k]
            return out

    def by_tier(self) -> dict[str, dict[str, int]]:
        """Totals rolled up per tier (T1/T2/T3) — the cost/quality mix at a glance."""
        with self._lock:
            out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(_FIELDS, 0))
            for (_node, tier), row in self._rows.items():
                for k in _FIELDS:
                    out[tier][k] += row[k]
            return {t: dict(v) for t, v in out.items()}

    def render(self) -> str:
        """A plain-text token manifest: per-node rows + per-tier + totals, with a
        coding-vs-orchestration rollup when the coder reported usage (tier ``code``)."""
        with self._lock:
            rows = sorted(
                self._rows.items(), key=lambda kv: kv[1]["output"], reverse=True
            )
            coding_out = sum(
                r["output"] for (_n, tier), r in self._rows.items() if tier == _CODE_TIER
            )
            has_coder = any(tier == _CODE_TIER for (_n, tier) in self._rows)
        totals = self.totals()
        tot_out = totals["output"] or 1  # avoid /0; a run with zero output is degenerate

        def pct(n: int) -> str:
            return f"{100 * n / tot_out:5.1f}%"

        if has_coder:
            head = ["TOKENS-MANIFEST — model spend by node, output-weighted (coder included)."]
        else:
            head = [
                "TOKENS-MANIFEST — PROXY (orchestration) spend by node, output-weighted.",
                "NOTE: excludes the coding substrate (its tokens are external to the proxy);",
                "run a real coding backend to include the coder (tier 'code').",
            ]
        lines = [
            *head,
            "",
            f"{'node':28} {'tier':5} {'calls':>6} {'in':>10} {'out':>10} "
            f"{'cache_rd':>10} {'out%':>6}",
            "-" * 84,
        ]
        for (node_id, tier), r in rows:
            lines.append(
                f"{node_id[:28]:28} {tier:5} {r['calls']:>6} {r['input']:>10} "
                f"{r['output']:>10} {r['cache_read']:>10} {pct(r['output']):>6}"
            )
        lines += ["-" * 84]
        for tier in sorted(self.by_tier()):
            tr = self.by_tier()[tier]
            lines.append(
                f"{'  tier ' + tier:28} {'':5} {tr['calls']:>6} {tr['input']:>10} "
                f"{tr['output']:>10} {tr['cache_read']:>10} {pct(tr['output']):>6}"
            )
        total_label = "TOTAL" if has_coder else "TOTAL (proxy only)"
        lines += [
            "-" * 84,
            f"{total_label:28} {'':5} {totals['calls']:>6} {totals['input']:>10} "
            f"{totals['output']:>10} {totals['cache_read']:>10} {'100.0%':>6}",
        ]
        if has_coder:
            lines.append(
                f"coding substrate: {pct(coding_out)} of output tokens; "
                f"orchestration (everything else): {pct(tot_out - coding_out)}"
            )
        return "\n".join(lines)
