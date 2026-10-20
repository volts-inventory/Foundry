"""Quality & Verification layer (agents.md Layer 4) — node factories.

* :func:`make_reviewer_node` — the ONE quality node of the build pipeline: a
  single ``rt.proxy.judge`` review verdict (proxy-stamped binding, server-issued
  nonce; the agent never mints a verdict, L5) plus the ``TestPlan`` / NEGATIVE
  ``OracleBinding`` per high-risk criterion / ``TestReport`` / ``E2EReport``
  records, derived deterministically from the acceptance criteria.
* :func:`make_reviewer_critic_node` — the verdict-only evaluator the gated-mode
  assurance subgraph runs (distinct node id, so its nonce never collides with the
  quality reviewer's in the same run — H3).
"""

from __future__ import annotations

from foundry.agents.quality.reviewer import make_reviewer_node
from foundry.agents.quality.reviewer_critic import make_reviewer_critic_node

__all__ = [
    "make_reviewer_critic_node",
    "make_reviewer_node",
]
