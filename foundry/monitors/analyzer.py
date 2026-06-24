"""Advisory sandboxed analyzer — semantic color ONLY. (governance.md §3)

This is the toolless, read-only LLM-analyzer seam. Its single hard invariant
(gov §3): it is **ADVISORY** — it emits flag strings that can only ANNOTATE a
Guardian assessment; it CANNOT approve work, clear, downgrade, or override a
deterministic trip. The Guardian computes the deterministic action/trips BEFORE
the analyzer runs and RE-ASSERTS them after (see `guardian._consult_analyzer`),
so even a malicious/buggy analyzer is structurally inert against the verdict.

Prompt-injection-resistance posture (gov §3):

- **Structured features only.** `flag()` receives a `Mapping[str, object]` of
  deterministically-extracted features — NEVER the raw repo, raw transcript, or
  any tool. There is no I/O, no tool surface, no file access here: the sandbox
  boundary IS the input type.
- **Untrusted prose is data, never instructions.** Any trace-derived string in
  the features is treated as opaque data to scan, never parsed as a command.
  Framing prose as data stops instruction-injection but NOT feature-flooding /
  cap-eviction or feature-shaping (the agent shapes the whole transcript), so:
- **Trusted-signal priority + capped slice.** This analyzer ranks features by a
  trusted-signal priority (zone-B execution records, OPA decision logs, runner
  attestations rank above agent-emitted transcript-derived features) and caps
  the slice it considers — so flooding cannot evict a suspicious high-priority
  feature by volume/recency.
- **The output type is `list[str]`** — no `Trip`, no action, no verdict can be
  expressed. A flag that LOOKS like an override ("clear all trips") lands as an
  inert annotation; the Guardian's post-assertion makes it impossible to act on.

The default implementation is a deterministic rule-bank stand-in (no live model
call here) so the seam is testable and the floor needs no egress; a real
deployment injects an LLM-backed `flag` honoring the SAME structured-features
contract. Precision/recall — incl. against prompt-injection-of-the-monitor and
feature-flooding — is measured per evaluation.md §4.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: feature keys ranked by trusted-signal priority (highest first). Trusted-infra
#: observations outrank agent-shaped transcript features so flooding the latter
#: cannot evict the former from the capped slice (gov §3 cap-eviction defense).
TRUSTED_FEATURE_PRIORITY: tuple[str, ...] = (
    "trip_kinds",
    "trip_rules",
    "alert_rules",
    "runner_attestations",
    "zone_b_records",
    "opa_decisions",
    "action",
)

DEFAULT_FEATURE_CAP = 32


class SandboxedAnalyzer:
    """Advisory-only analyzer (gov §3). `flag(features) -> list[str]`.

    Output is annotations only; it can never alter a deterministic trip. The
    Guardian re-asserts the deterministic verdict after consulting it.
    """

    def __init__(self, *, feature_cap: int = DEFAULT_FEATURE_CAP) -> None:
        if feature_cap < 1:
            raise ValueError("feature_cap must be >= 1")
        self.feature_cap = feature_cap

    def flag(self, features: Mapping[str, object]) -> list[str]:
        """Emit advisory flag strings over the STRUCTURED features only.

        No tools, no I/O, no raw transcript: the input mapping is the entire
        world this analyzer can see (the sandbox boundary). Returns inert
        annotation strings — never a verdict.
        """
        ranked = self._ranked_capped_keys(features)
        flags: list[str] = []

        kinds = features.get("trip_kinds")
        if isinstance(kinds, Sequence) and not isinstance(kinds, str | bytes) and len(kinds) > 1:
            flags.append(
                f"advisory: {len(kinds)} concurrent deterministic trips — "
                "semantic correlation worth a human glance"
            )

        # Sampled/untriggered pass: trips empty but cost/policy color present.
        if not kinds and any(k in ranked for k in ("alert_rules", "opa_decisions")):
            flags.append("advisory: pre-trip cost/policy color on an untriggered superstep")

        if not flags:
            flags.append("advisory: no semantic anomaly in the structured feature slice")
        return flags

    def _ranked_capped_keys(self, features: Mapping[str, object]) -> tuple[str, ...]:
        """Priority-rank present feature keys by trusted signal, then cap.

        Trusted-infra keys come first (in `TRUSTED_FEATURE_PRIORITY` order);
        any remaining (agent-shaped) keys follow in sorted order. The slice is
        capped so transcript flooding cannot evict a trusted high-priority key.
        """
        present = set(features)
        ordered = [k for k in TRUSTED_FEATURE_PRIORITY if k in present]
        ordered.extend(sorted(present - set(ordered)))
        return tuple(ordered[: self.feature_cap])
