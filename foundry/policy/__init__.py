"""The five-plane guardrail policy package. (governance §2, implementation-plan §5.3)

Models the five guardrail planes (Policy / Execution / Data / Observability /
Assurance) as an explicit, inspectable registry; evaluates run-immutable
OPA-style policy fail-closed; pins a content-addressed run-immutable scanner
config that gate verdicts bind to; and enforces segregation of duties (an actor
cannot both produce and sign/admit). Dev-grade in-process evaluator
(implementation-plan §0.3): production swaps in OPA/Conftest/Checkov as an ops
change — the fail-closed seam is what is real here.
"""

from foundry.policy.authorization import (
    AuthorizationDecision,
    Decision,
    DutyRequest,
    Zone,
    authorize_segregation_of_duties,
)
from foundry.policy.planes import (
    DENY_RULES,
    FIVE_PLANE_MODEL,
    DenyRule,
    FivePlaneModel,
    Plane,
    PlaneSpec,
    PolicyDecision,
    PolicyEvaluator,
)
from foundry.policy.scanner_config import (
    PinnedScannerConfig,
    ScannerConfigRecord,
    ScannerConfigTamperError,
    ScannerRule,
)

__all__ = [
    "DENY_RULES",
    "FIVE_PLANE_MODEL",
    "AuthorizationDecision",
    "Decision",
    "DenyRule",
    "DutyRequest",
    "FivePlaneModel",
    "PinnedScannerConfig",
    "Plane",
    "PlaneSpec",
    "PolicyDecision",
    "PolicyEvaluator",
    "ScannerConfigRecord",
    "ScannerConfigTamperError",
    "ScannerRule",
    "Zone",
    "authorize_segregation_of_duties",
]
