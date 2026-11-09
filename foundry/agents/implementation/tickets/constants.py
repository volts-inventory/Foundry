"""Node id, artifact kinds, triage names and budgets of the engineering node."""

from __future__ import annotations

from foundry.schemas.implementation import Specialty

NODE_ID = "engineering"
TICKET_KIND = "ticket"
CONTRACT_KIND = "interface_contract"
_CRITERION_KIND = "acceptance_criterion"
_FEATURE_DIFF_KIND = "feature_diff"
# Triage names this module writes; SKIP doubles as the "never built" marker.
SKIP_TRIAGE = "ticket-skipped"
SCOPE_TRIAGE = "write-scope"
# The fixer's kept writes outside the built tickets' files, and each contract file
# it changed (the agreed signatures drift from the plan only through these).
FIX_SCOPE_TRIAGE = "fix-scope"
CONTRACT_CHANGE_TRIAGE = "contract-change"
# One note per failed attempt (the triage union keys on name, so the attempt is in it).
FAIL_TRIAGE = "ticket-failed"
CAP_TRIAGE = "token-cap"
OVERLAY_TRIAGE = "wave-overlay"
# A ticket declared a type the contracts file already defines (a parallel copy).
CONTRACT_DUP_TRIAGE = "contract-duplicate"
# The contracts ticket's last attempt still left out names its planned contracts declare.
CONTRACT_DRIFT_TRIAGE = "contract-drift"
# An implementation ticket's last attempt still changed a stubbed public signature.
SIGNATURE_CHANGE_TRIAGE = "signature-change"
SKELETON_TRIAGE = "skeleton"
# A failed attempt's written files its retry starts from (explanation: their snapshot blob).
CARRY_TRIAGE = "ticket-carry"
# Attempts a ticket gets before it is skipped; bounds the loop on a ticket that
# keeps erroring.
MAX_TICKET_ATTEMPTS = 2

# Per-specialty concurrent ticket limit when none is configured.
DEFAULT_WORKERS: dict[str, int] = {s.value: 4 for s in Specialty}

# Claude Code needs several turns to read, write and verify; 8 left contract stubs empty.
_DEFAULT_TURN_BUDGET = 25
# A local session takes one action per turn and re-reads what masking elided: on a
# local notes build 8 of 14 tickets hit 25 turns, one before it wrote its step module.
_LOCAL_TURN_BUDGET = 40
_DEFAULT_COST_BUDGET_MICROUSD = 1_000_000
