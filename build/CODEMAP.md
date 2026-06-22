# CODEMAP — living index (regenerated each milestone)

Regenerated 2026-06-22T19:48:51 — end of Phase 4 (M4, Trust root & Autonomous mode).

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
## tests: 75 files | foundry: 135 modules, 25910 LOC | coverage 94%
## Phase-4 trust root (foundry/trust_root/ + hardening): runner zone-B config re-derivation + sealed
   observation (forged-results); attestation input-bound + provenance binding (forged-verdict/replay);
   gates/discriminator held-out-mutant+behavioral-invariance (malicious-test); trusted_builder hermetic
   SLSA-L3 (pinned-only, reproducible, signed provenance); admission fail-closed 4-check; deployer
   non-agent idempotent rollback; global_records digest-disqualification (fork-divergence); operations/
   trusted_lifecycle key-rotation+HA; supervisor/factory_graph MODE_AUTONOMOUS (admission = sole gate).
