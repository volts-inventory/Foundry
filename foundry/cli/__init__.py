"""Foundry CLI — run a one-line request end-to-end through the factory graph.

(SPEC.md "SMOKE-TEST CORE PATH"; implementation-plan §0.3, §7; T-M1-15)

This is the Phase-1 smoke-test entry point: from a single natural-language
request it assembles the FULL dev wiring with the test doubles (NO live API key,
NO ``claude -p`` binary, NO network), compiles the factory graph, and runs the
SPEC core path::

    triage -> planner -> [spec gate] -> implementation
           -> quality -> [build+test gates] -> emit (PR-equivalent action)

end to end, returning the emitted reviewed-artifact handle, the single ``open_pr``
:class:`ExternalAction` (its idempotency key + payload uri), and the attested
gate statuses.

The dev wiring (LEARNINGS D1-D6, AS1-AS5, FG1-3, GW1-3, SUP1-7) — mirrors the
proven assembly in ``tests/test_factory_graph.py`` / ``tests/test_supervisor.py``:

* an in-memory :class:`ArtifactStore` (``rt.cas``) — the real content-addressed
  body store written on emit (AS1-AS5);
* an :class:`AgentRuntime` whose model egress is a real
  :class:`ModelProviderProxy` backed by a :class:`MockTransport` (deterministic,
  no live API key, L6/D1) + a dev :class:`ZoneBSigner`, and whose coding
  substrate is a :class:`StubAdapter` (no ``claude -p``, D3);
* a real :class:`TwoZoneGateRunner` (dev signer + :class:`GlobalRecords` +
  scanner/lineage revs) — the SOLE minter of signed verdicts (L5/GW2);
* a :class:`DevGateWriteVerifier` installed on the ``merge_gates`` contextvar so
  the REAL reducer admits the runner-attested verdicts (D4) — its token is reset
  in a ``finally`` so it never leaks;
* a dev :class:`FoundryCheckpointer` so the run is durable + resumable;
* the materialization seams ``resolve`` / ``resolve_blob`` (GW1a/D-R1-02). The
  StubAdapter authors a deterministic no-op blob (not a passing test suite), so
  to drive the SPEC core path to a clean Spec/Build/Test PASS the smoke run uses
  the SAME KIND-dispatching synthetic resolver the end-to-end tests use (SUP7):
  it synthesizes a faithful body per ``handle.kind`` and a passing unified-diff
  blob. The real :meth:`ArtifactStore.resolve` / :meth:`ArtifactStore.resolve_blob`
  views are wired as the default fallthrough so a future adapter that authors a
  real passing suite needs no CLI change.

ALL model egress stays behind the proxy (L6) — this module never imports
``anthropic`` and never touches GitHub / the network: the PR is expressed ONLY
as an :class:`ExternalAction` on the ``actions`` channel (FG2).
"""

from __future__ import annotations

from foundry.cli.command import main, parse_ticket_tiers, parse_workers
from foundry.cli.defaults import (
    _MOCK_BACKEND,
    ADAPTERS,
    AUTO_SLOTS_CAP,
    BACKENDS,
    MODES,
    SCOPES,
    _default_adapter_for,
    env_local_model,
    env_local_url,
    env_max_slots,
)
from foundry.cli.doubles import (
    _DIFF_BODY_URI,
    _PASSING_DIFF,
    _SCRIPTS,
    _judgment_anchors,
    _materialization_seam,
    _synthetic_resolve,
    _synthetic_resolve_blob,
)
from foundry.cli.invoke import RunCrashed, _invoke_with_autoresume, _max_epoch
from foundry.cli.output import _write_draft_on_halt, _write_output_tree
from foundry.cli.result import RunResult
from foundry.cli.runner import (
    _LINEAGE_REV,
    _SCANNER_REV,
    _detailed_artifacts,
    fix_until_cap_for,
    run,
)
from foundry.cli.spec import _SPEC_MAX_BYTES, _load_spec, _request_from_spec
from foundry.cli.wiring import (
    _KEY,
    _KEY_ID,
    _build_proxy,
    _default_tier_models,
    _resolve_coding_adapter,
    _tier_models,
    make_coding_adapter,
)

__all__ = [
    "ADAPTERS",
    "AUTO_SLOTS_CAP",
    "BACKENDS",
    "MODES",
    "SCOPES",
    "_DIFF_BODY_URI",
    "_KEY",
    "_KEY_ID",
    "_LINEAGE_REV",
    "_MOCK_BACKEND",
    "_PASSING_DIFF",
    "_SCANNER_REV",
    "_SCRIPTS",
    "_SPEC_MAX_BYTES",
    "RunCrashed",
    "RunResult",
    "_build_proxy",
    "_default_adapter_for",
    "_default_tier_models",
    "_detailed_artifacts",
    "_invoke_with_autoresume",
    "_judgment_anchors",
    "_load_spec",
    "_materialization_seam",
    "_max_epoch",
    "_request_from_spec",
    "_resolve_coding_adapter",
    "_synthetic_resolve",
    "_synthetic_resolve_blob",
    "_tier_models",
    "_write_draft_on_halt",
    "_write_output_tree",
    "env_local_model",
    "env_local_url",
    "env_max_slots",
    "fix_until_cap_for",
    "main",
    "make_coding_adapter",
    "parse_ticket_tiers",
    "parse_workers",
    "run",
]
