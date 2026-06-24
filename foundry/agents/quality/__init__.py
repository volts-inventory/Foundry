"""Quality & Verification layer (agents.md Layer 4) — node factories.

Three ``make_<agent>_node(rt: AgentRuntime, ...) -> NodeFn`` factories:

* :func:`make_reviewer_critic_node` — the optimizer's evaluator. The ONE
  quality node that touches the ``gates`` channel, and only via the trusted
  ``rt.proxy.judge`` path: the proxy extracts ``status`` from the constrained
  verdict channel and stamps the binding (the agent holds no key), returning a
  ``GateResult`` ``_merge_gates`` admits on a valid binding + fresh server-issued
  ``verdict_nonce`` + no negative latch. The agent never mints a verdict (L5).
* :func:`make_qa_engineer_node` — authors a ``TestPlan`` (``test_id ->
  criterion_ids`` traceability) with a NEGATIVE ``OracleBinding`` per high-risk
  criterion (fidelity, not coverage) plus a ``TestReport`` recording the N=2
  fail-then-pass quarantine. Emits content-addressed handles to ``artifacts``.
* :func:`make_sdet_node` — authors an ``E2EReport`` with pinned-seeds / frozen-
  clock / mocked-network determinism notes, scenario→criterion traceability,
  and the flake quarantine. Emits a handle to ``artifacts``.

Every node follows the shared seam (LEARNINGS AR1-AR4): mint a deterministic
``Seq`` via ``rt.seq``, reach the single egress through ``rt.proxy`` (L6),
content-address bodies behind ``cas://`` handles (DS9 / L4). Only the reviewer
writes ``gates`` — and only the proxy-stamped ``judge()`` result (L5).
"""

from __future__ import annotations

from foundry.agents.quality.qa_engineer import make_qa_engineer_node
from foundry.agents.quality.reviewer_critic import make_reviewer_critic_node
from foundry.agents.quality.sdet import make_sdet_node

__all__ = [
    "make_qa_engineer_node",
    "make_reviewer_critic_node",
    "make_sdet_node",
]
