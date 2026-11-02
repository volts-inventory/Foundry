"""Quality & Verification layer (agents.md Layer 4) — node factories.

* :func:`make_reviewer_node` — the ONE quality node of the build pipeline: a
  single ``rt.proxy.judge`` review verdict (proxy-stamped binding, server-issued
  nonce; the agent never mints a verdict, L5) plus the ``TestPlan`` / NEGATIVE
  ``OracleBinding`` per high-risk criterion / ``TestReport`` / ``E2EReport``
  records, derived deterministically from the acceptance criteria.
"""

from __future__ import annotations

from foundry.agents.quality.reviewer import make_reviewer_node

__all__ = [
    "make_reviewer_node",
]
