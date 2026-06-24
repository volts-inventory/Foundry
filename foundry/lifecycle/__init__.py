"""Product-lifecycle modules — maintenance + provenance. (docs/lifecycle.md §1/§5)

The pipeline ships a release; the LIFECYCLE owns software over its life. Two
concerns live here, both fail-closed and both REUSING the existing trust seams
rather than reinventing them:

* :mod:`foundry.lifecycle.maintenance` — a maintained/evolved change to a
  previously-released artifact is RE-VERIFIED change-proportionally (reusing
  :func:`foundry.graph.reverification.reverification_plan`) and must RE-PASS the
  trust gates + RE-ADMIT before the maintained version is released. Maintenance
  NEVER bypasses the gates (fail-closed: a maintained change that does not
  re-pass is not released). Provenance of the maintained version links to the
  prior version — a maintenance chain.

* :mod:`foundry.lifecycle.provenance` — every released artifact TRACES to its
  run + attested gate verdicts + evidence pack + sign-offs + SLSA provenance,
  and the trace is TAMPER-EVIDENT (reusing the evidence-pack hash-chain). A
  released artifact with a broken/incomplete provenance chain is FLAGGED, never
  trusted as released (fail-closed).

This package is import-cheap (no heavy imports at module import time); the
feedback loop (T-M6-10) and the exit test consume these modules.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
