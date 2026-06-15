# DECISIONS — append-only

## D1 — Remove the existing GitHub CI workflow (orchestrator constraint)
**2026-06-15.** The repo shipped `.github/workflows/ci.yml` (lint + pytest on push/PR). The
orchestrator operating constraint forbids GitHub CI in any form. Removed the workflow and the
`.github/` tree. All verification runs locally through the gate chain in ARCHITECTURE.md.
The design docs' references to "evals shipped in CI" are honored as *local* gates, not Actions.
Reversible: re-add the workflow if the no-CI constraint is ever lifted.

## D2 — Type checker = mypy, scoped to `foundry/` (production code)
**2026-06-15.** Chosen over pyright (no node dependency; pure-Python, installs via uv). The
existing 6.7k-LOC backbone produced only 4 mypy errors under `ignore_missing_imports` — all
fixed. Strict bar applies to `foundry/` only; test modules use dynamic pydantic/hypothesis
patterns that fight strict typing for little safety gain. Recorded as a QUALITY BAR.

## D3 — Coverage bar 80% overall / mutation 70% scoped
**2026-06-15.** Current baseline is 82% line+branch coverage on the backbone. Set the milestone
floor at 80% (achievable, non-regressive) with new code targeting ≥85%. Mutation testing
(mutmut) runs at milestone gates scoped to changed modules at a ≥70% kill bar — full-repo
mutation is too slow for per-wave use (resource discipline).

## D4 — Treat the in-repo design docs as the authoritative SPEC/ARCHITECTURE
**2026-06-15.** The repo already contains a converged, multi-round-reviewed design + a
build-ready implementation plan with a phase backlog and topological build order. Per the
orchestrator's "respect any stack already implied," `build/SPEC.md` and `build/ARCHITECTURE.md`
are thin orchestration pointers over those docs (acceptance criteria, quality bars, interface
contracts) rather than rewrites. Phases 0–6 of implementation-plan §7 are the milestone spine.

## D5 — Operating profile: single-lineage self-judge, zero-HITL, autonomous
**2026-06-15.** Adopted from implementation-plan §0.2 (the doc's chosen profile). Machine gates
stay fail-closed; judgment seams stamp `judgment_provenance="self"`. The decorrelation/HITL
machinery is still built so the regulated pack can force HITL and operators can flip back.
This matches the autonomous one-shot build mandate.
