# EXPERTS — domain expert profiles (curated; appended as domains surface)

Seed set; a fresh Expert Curator expands this from the design docs. Each profile: scope,
standards, when consulted, and whether it serves as IMPLEMENTER and/or CRITIC. Domain-heavy
tasks are built by a fresh expert-implementer and critiqued by a DIFFERENT fresh expert-critic.

## E1 — LangGraph / orchestration engineer
- Scope: `StateGraph` assembly, nodes/conditional edges, checkpointing, interrupts, supervisor
  routing, subgraph composition, durable resume semantics.
- Standards: pure-ish nodes return channel writes; no hidden global mutation; resume-idempotent.
- Consulted: graph/, agents wiring, supervisor. Implementer + Critic.

## E2 — Distributed-systems / durability engineer
- Scope: transactional outbox, one-txn/superstep, at-least-once relay + idempotency, WORM audit
  chain, checkpointer, cross-fork records/latches.
- Standards: fail-closed, idempotent resume, no dual-write divergence (build-log B3).
- Consulted: durability/, state/global_records. Implementer + Critic.

## E3 — Security / trust-root engineer
- Scope: two-zone gate runner, attestation/judgment binding, SLSA/SBOM, admission, deployer,
  sandbox isolation, supply-chain policy. Self-judge provenance.
- Standards: fail-closed admission; verdicts only from trusted infra; secrets never in agent zone.
- Consulted: gates/, trust_root/, execution/sandbox, security schemas. Implementer + Critic.

## E4 — LLM-application / agent engineer (Anthropic)
- Scope: model-provider proxy, agent SOPs, prompt/IO contracts, determinism policy, tier mix,
  worker-substrate adapters. MUST verify Anthropic API surface against LEARNINGS P1–P4, not memory.
- Standards: all egress via proxy; structured (json_schema) outputs for judgments; temp policy.
- Consulted: proxy/, agents/, execution/adapters. Implementer + Critic.

## E5 — Statistics / evaluation methodologist
- Scope: A/B pre-registration, power/MDE, decision table, scorecard, Holm–Bonferroni, alpha
  ledger, ITT reporting, eval-integrity (hidden-test unreachability, contamination probes).
- Standards: pre-registration binding; no post-hoc rung selection; self-judge never reported as
  independent validation.
- Consulted: evaluation/, domains class-3 gates. Implementer + Critic.

## E6 — Pydantic / typed-contracts engineer
- Scope: frozen pydantic v2 artifact models, versioning, validation, schema cohesion across layers.
- Standards: every agent I/O is a validated frozen model; supersession not mutation.
- Consulted: schemas/. Implementer + Critic (often paired with the domain owner).
