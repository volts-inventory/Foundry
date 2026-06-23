# CODEMAP — living index (regenerated each milestone)

Regenerated 2026-06-22T21:24:34 — end of Phase 5 (M5, Specialist plugins + domain packs).

## foundry/ packages
```
foundry/ (3 modules)
foundry/agents/ (3 modules)
foundry/agents/architecture/ (3 modules)
foundry/agents/implementation/ (3 modules)
foundry/agents/product/ (6 modules)
foundry/agents/quality/ (5 modules)
foundry/agents/security/ (4 modules)
foundry/context/ (4 modules)
foundry/domains/ (6 modules)
foundry/domains/packs/ (4 modules)
foundry/durability/ (6 modules)
foundry/evaluation/ (11 modules)
foundry/execution/ (3 modules)
foundry/execution/adapters/ (4 modules)
foundry/gates/ (11 modules)
foundry/governance/ (6 modules)
foundry/graph/ (7 modules)
foundry/graph/subgraphs/ (6 modules)
foundry/monitors/ (10 modules)
foundry/observability/ (3 modules)
foundry/operations/ (9 modules)
foundry/policy/ (4 modules)
foundry/proxy/ (4 modules)
foundry/schemas/ (10 modules)
foundry/state/ (6 modules)
foundry/trust_root/ (4 modules)
```
## tests: 84 files | foundry: 145 modules, 28889 LOC | coverage 94%
## Phase-5 domains (foundry/domains/): contract (7 tighten-only hooks) + registry (capability routing,
   compose strictest-wins, open/closed register) + verdict_class (classes 1-5 fail-closed) + state_sha
   (ML data/weight key + compute budget) + governance_hooks (mode/tier veto, approver, egress, perceptual
   HITL); packs/{regulated (autonomous-vetoed+approver+class-4), ml_data (state_sha class-3+alpha+compute),
   frontend (class-5 visual-HITL + sub-pixel envelope + WCAG)}. Engaged at triage via pack_registry; a new
   pack adds via register() with zero core edits.
