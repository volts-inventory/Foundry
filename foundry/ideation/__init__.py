"""Front-of-funnel ideation / brainstorm mode — co-generate a design spec.

This package is an LLM-driven *authoring aid*: given a rough one-line idea it
runs a bounded, iterative ideation loop through ``rt.proxy.complete`` (the
single model egress, LEARNINGS L6 — no new credential or egress, it inherits the
swappable backend) to ask clarifying questions, propose capabilities, sketch
acceptance criteria and a rough architecture, and refine over a few rounds. The
deliverable is a rendered markdown SPEC DOCUMENT that a human can read, edit, and
then feed to a build via ``foundry.cli.run(spec=...)`` / ``--spec FILE`` (task
[8]'s ingest seam).

ADVISORY ONLY (the load-bearing trust posture). The ideation output INFORMS a
build's spec; it NEVER overrides the spec gate, the criteria-completeness
derivation, the criteria-critic, or any signed verdict. It is a front-of-funnel
authoring aid, not a trust input. Concretely:

* it produces ONLY plain text/markdown (an :class:`IdeationResult` + its rendered
  spec) — it writes no ``GateResult`` / ``GateTriage`` / ``JournalEntry``, mints
  no ``Seq``, and has NO channel-write surface (it never touches ``FactoryState``
  or any reducer);
* when its markdown is later ingested via ``--spec``, it lands on the additive
  ``spec`` channel as plain input data — the product nodes GROUND their intent in
  it, but the acceptance-criteria / spec-gate machinery downstream is unchanged
  (agents propose, only signed gates decide, L5).

The module is purely-deterministically testable: inject a
:class:`~foundry.proxy.model_provider_proxy.ModelProviderProxy` over a
:class:`~foundry.proxy.test_doubles.MockTransport` (no live API key, no network).
"""

from __future__ import annotations

from foundry.ideation.loop import (
    IDEATION_SCHEMA,
    IdeationResult,
    IdeationRound,
    ideate,
    render_spec_markdown,
)

__all__ = [
    "IDEATION_SCHEMA",
    "IdeationResult",
    "IdeationRound",
    "ideate",
    "render_spec_markdown",
]
