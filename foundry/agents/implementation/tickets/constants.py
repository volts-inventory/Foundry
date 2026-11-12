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
# The entrypoint ticket redefined a function or class another module of the tree owns.
ENTRYPOINT_DUP_TRIAGE = "entrypoint-redefinition"
# The contracts ticket's last attempt still left out names its planned contracts declare.
CONTRACT_DRIFT_TRIAGE = "contract-drift"
# An implementation ticket's last attempt still changed a stubbed public signature.
SIGNATURE_CHANGE_TRIAGE = "signature-change"
# One note per attempt (build or fix-loop rebuild) the stub-signature guard rejected;
# the attempt number is in the name, as for FAIL_TRIAGE.
SIGNATURE_REJECTED_TRIAGE = "signature-rejected"
# A ticket whose attempts the guard rejected REPLAN_AFTER times: its planned interface
# is likely wrong, so it is left to a re-plan instead of rebuilt again.
REPLAN_TRIAGE = "ticket-replan"
REPLAN_AFTER = 2
# The planner's answer to a REPLAN note, at most one per ticket per run: it revised
# the ticket's contracts (which supersedes the ticket's earlier notes here, by
# emission epoch, so it is built afresh) or declined to.
PLANNER_NODE_ID = "planner"
INTERFACE_REVISED_TRIAGE = "interface-revised"
INTERFACE_DECLINED_TRIAGE = "interface-declined"
# A ticket that failed after its retry (and its escalation, in hybrid) and is large
# enough to split (ADaPT: decompose only where the executor failed): the planner is
# asked once per ticket to split it, and answers SPLIT (the children replace it) or
# SPLIT_DECLINED (the fix loop's restart stays its fallback). Each child gets a
# SPLIT_CHILD note (a child is never split again) naming the snapshot blob of the
# parent's failed attempt it starts from, or empty to start from the integrated tree.
SPLIT_REQUEST_TRIAGE = "ticket-split-request"
SPLIT_TRIAGE = "ticket-split"
SPLIT_DECLINED_TRIAGE = "split-declined"
SPLIT_CHILD_TRIAGE = "split-child"
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
