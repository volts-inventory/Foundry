# EXECUTION PLAN — post-build feature & hardening waves

> Living reference for executing the 15-task backlog safely, in parallel where
> possible. Update the **Status log** and per-task **status** boxes as work lands.
> Source of truth for *order, dependencies, file-ownership, and the critic loop*.

Branch: `claude/autonomous-delivery-orchestrator-43wylm`. The base system is
COMPLETE and green (ruff/mypy clean, full suite passing); this plan adds features
and hardens recent additions without regressing that baseline.

---

## 0. The critic loop (MANDATORY for every addition)

Every task ("addition") passes through this loop before it is considered done.
Implementer ≠ critic (fresh eyes), per the build's standing discipline.

1. **Implement** the task in an isolated git **worktree** (one per lane) against
   its acceptance criteria.
2. **Self-gate:** `uv run ruff check foundry tests` · `uv run mypy foundry` ·
   `uv run pytest <scoped tests>` — all green.
3. **Critic pass:** dispatch a FRESH critic agent (not the implementer) to review
   the diff against (a) the design docs, (b) the task's acceptance criteria,
   (c) trust-model regressions, (d) integration risk. Critic returns findings
   ranked BLOCKER / MAJOR / MINOR.
4. **Fix:** if any BLOCKER/MAJOR (or actionable MINOR) — dispatch a fixer, resolve,
   re-run gates, and re-critic if the change was substantial. Loop until the
   critic is clean or only documented/accepted MINORs remain.
5. **Integrate:** merge to the branch only when critic-clean AND gates green.
   Then run the **full suite** (`uv run pytest -q -p no:cacheprovider`) as the
   integration check — must stay green.
6. **Record:** mark the task `completed` (TaskUpdate), tick it in the Wave table
   below, and append a line to the **Status log** (§5).

Rule: a task is NOT done if gates fail, the critic has an unresolved
BLOCKER/MAJOR, the implementation is partial, or the full suite regressed.

---

## 1. Safety model (what makes parallel execution safe)

1. **One subsystem per lane** — a lane owns a file region so two lanes never edit
   the same file in the same wave.
2. **Worktree isolation** — each parallel lane runs in its own git worktree.
3. **Gates-green-before-merge** + critic loop (§0) per task.
4. **The `cli.py`+graph spine is serial** — tasks [1][3][8][9][10][12] all touch
   `cli.py`/`factory_graph.py`; they form one ordered critical path. Their
   *non-cli* work may be built in parallel and integrated one at a time.
5. **Integrate in wave order** — a wave merges fully (full suite green on branch)
   before the next wave starts.
6. **Known contention trap:** [3] and [7] both edit `foundry/docs/manifest.py` —
   they are split across Wave 2 / Wave 3 and must never run concurrently.

---

## 2. Lanes (subsystem owners)

| Lane | Owns (file region) | Tasks (serial within lane) |
|------|--------------------|----------------------------|
| **Spine** | `foundry/cli.py`, `foundry/graph/{factory_graph,supervisor}.py` | [1] → [3] → [8] → [9] → [10] → [12] |
| **Proxy** | `foundry/proxy/*` | [5] → [6] → [7] |
| **Gates** | `foundry/gates/spec.py`, `agents/product/criteria_critic.py` | [4] |
| **Authoring** | `agents/product/product_manager.py`, `agents/architecture/principal_architect.py`, `docs/render.py` | [2] |
| **Journal** | `agents/_runtime.py`, `agents/_common.py`, agent nodes, `state/` channel | [11] |
| **Infra** | `docker-compose.yml`, Dockerfiles, `durability/artifact_store.py` (fs backend), `durability/checkpointer.py` | [14] → [15] |
| **Ideation** | new `foundry/ideation/` module | [13] |

---

## 3. Waves (each row runs concurrently; advance only when the wave is merged & green)

| Wave | Spine | Proxy | Gates / Authoring / Journal | Infra / Ideation | Status |
|------|-------|-------|------------------------------|------------------|--------|
| **1** | [1] Output dir | [5] Backend determinism | [4] Spec-gate critic · [2] Prose bodies | [14] Compose profile | ✓ DONE |
| **2** | [3] Doc-node *(needs [1])* | [6] CLI parser hardening | [11] Agent journal | [14] cont. | ✓ DONE |
| **3** | [8] Spec ingestion | [7] Drift actionable | — | ([15] deferred to a later wave) | ✓ DONE |
| **4** | [9] `--mode`/`--packs` *(needs [8])* | proxy done | — | [13] ideation *module* *(needs [8])* | ✓ DONE |
| **5** | [10] Checkpoint/HITL *(needs [9])* | — | — | [13] cli integration | ☐ |
| **6** | [12] Evolution route *(needs [1]+[8])* | — | — | — | ☐ |

**Critical path:** [1]→[3]→[8]→[9]→[10]→[12] (~6 serial integrations). All other
lanes drain by Wave 3.

---

## 4. Task register (id · order · deps · files · acceptance criteria · status)

> `order` = the [N] tag. `#` = TaskList id. Status: ☐ pending / ◐ in progress / ✓ done.

### [1] Output dir — #2 · deps: none · ✓ (Wave 1; critic CLEAN; merged)
Files: `cli.py`, reuse `graph/gate_wiring.materialize_workdir`, `RunResult`.
Accept: `run(..., output_dir=)` + `--out DIR` materializes the final accepted
integrated tree to disk on a passing build; path surfaced on `RunResult`; tests.

### [2] Persist prose bodies — #1 · deps: none · ✓ (Wave 1; critic CLEAN; merged)
Files: `agents/product/product_manager.py`, `agents/architecture/principal_architect.py`, `docs/render.py`.
Accept: PM/architect `store.put_body_at(uri, prose)` the prd/design/ADR bodies
they already generate; `render.py` inlines resolved prose instead of a dead
handle; tests.

### [3] Doc-assembly → graph node — #12 · deps: [1] · ✓ (Wave 2; critic CLEAN; merged)
Files: `docs/assemble.py`, `cli.py`, `graph/factory_graph.py`, `docs/manifest.py`.
Accept: doc assembly runs as a terminal graph node (budget-metered, checkpointed,
outbox-governed); `GateRef.attested` also counts a valid judgment_binding; narrow
the cli `except` around assembly; tests.

### [4] Spec gate honors criteria-critic — #7 · deps: none · ✓ (Wave 1; critic CLEAN + 2 MINORs fixed; merged)
Files: `gates/spec.py`, `agents/product/criteria_critic.py`.
Accept: critic-flagged security/abuse omission (zone-B-re-derivable) FAILS the
Spec gate; adversarial test: omission → Spec FAIL → no PR.

### [5] Backend-aware determinism — #8 · deps: none · ✓ (Wave 1; critic CLEAN; merged)
Files: `proxy/model_provider_proxy.py`, `proxy/cli_transports.py`, maybe `proxy/lineage.py`.
Accept: temperature-vs-effort decision is backend/lineage-aware; reasoning/OpenAI
models get effort, never a rejected temperature; T2/T3 CLI gate calls pin
determinism; per-backend tests.

### [6] CLI parser hardening — #9 · deps: [5] · ✓ (Wave 2; critic CLEAN; merged)
Files: `proxy/cli_transports.py`, tests/fixtures.
Accept: Codex extraction handles current `item.completed`/`agent_message` shapes
and raises `TransportError` on empty text at zero-exit; Claude handles
modelUsage-absent envelopes; real-shape fixtures in the suite.

### [7] Drift actionable — #10 · deps: [5] · ✓ (Wave 3; critic CLEAN; merged)
Files: `proxy/model_provider_proxy.py`, `observability/drift_alerts.py`, `docs/manifest.py`.
Accept: drift policy decided + implemented (fail-closed for gate/judgment or
explicitly advisory + live monitor wired); Ollama tier-mismatch surfaced; tests.

### [8] Spec-doc ingestion — #4 · deps: none · ✓ (Wave 3; critic CLEAN + 1 MINOR fixed; merged)
Files: `cli.py`, state init/intake, `agents/product/*`.
Accept: `--spec FILE` / `run(spec=)` ingests full design-spec doc(s) seeding
product/architecture (retrieved_spans / pre-seeded intent); tests. Unblocks [12][13].

### [9] `--mode`/`--packs` — #11 · deps: [8] · ✓ (Wave 4; adversarial critic CLEAN; merged)
Files: `cli.py`, `build_factory_graph` wiring (+ trust-root construction for gated/autonomous).
Accept: `--mode {assistant|gated|autonomous}` + `--packs ...`; gated/autonomous
construct the trust-root components; surface the per-mode trust posture; tests.

### [10] Checkpoint/HITL mode — #3 · deps: [9] · ☐
Files: `graph/supervisor.py`/`factory_graph.py`, governance sign-off wiring, `cli.py`, schemas.
Accept: selectable mode that pauses at stage boundaries, surfaces stage
artifacts/evidence, and resumes on human approve/reject (reuses existing Gated +
sign-off machinery); tests.

### [11] Agent journal + interview — #6 · deps: none · ✓ (Wave 2; adversarial critic CLEAN; merged)
Files: `agents/_runtime.py`, `agents/_common.py`, agent nodes, `state/` channel.
Accept: each node records a structured advisory rationale entry; checkpoint
"interview" re-instantiates role+journal via proxy. HARD: strictly advisory,
NEVER a trust input / gate influence. Cost-gated. Tests assert it cannot gate.

### [12] Evolution route — #13 · deps: [1] + [8] · ☐
Files: `cli.py`, `lifecycle/maintenance.py` wiring, `graph/factory_graph.py`.
Accept: `foundry evolve <repo> "<change>"` / `run(evolve_from=)` — change-
proportional re-verify, re-gate (same runner), re-admit fail-closed, provenance
chain to prior version, prior criteria/oracles carried as regression suite,
Guardian-gates edits to existing criteria; tests.

### [13] Ideation front-end — #5 · deps: [8] · ✓ (Wave 4; critic CLEAN + 1 MINOR fixed; merged)
Files: new `foundry/ideation/`, proxy, small `cli.py` integration.
Accept: interactive ideation loop (via rt.proxy) that co-generates a spec doc and
hands it to [8]'s ingest seam; ideation output is advisory, never overrides the
spec gate; tests.

### [14] Docker-compose profile — #14 · deps: none · ✓ (Wave 1; critic CLEAN; merged — testable parts; docker = scaffolding)
Files: `docker-compose.yml`, Dockerfiles, `durability/artifact_store.py` (fs backend), `durability/checkpointer.py`, env config.
Accept: volume-backed fs CAS + postgres checkpoints; proxy/signer/builder in
isolated containers (network policy, key as secret); pinned scanner service
containers; bundled OTel+Prometheus+Grafana+Loki. DoD: state behind interfaces,
config/secrets via env, EKS-migration note. Retires multi-tenant + HA/cloud-KMS
items for this profile.

### [15] Execution-worker pool — #15 · deps: [14] · ☐
Files: execution dispatch layer, worker entrypoint, queue/state.
Accept: orchestrator emits sandboxed execution tasks to durable state; a pool of
GENERIC sandbox workers (role from task, not container) claims+runs them with
hard-kill+limits; logical concurrency in code, physical slots in infra; NO in-app
container spawning; bounded by provider quota + budget. Tests.

---

## 5. Status log (append-only; newest last)

- _(not started)_ — plan authored; baseline green (ruff/mypy clean, full suite
  passing, 169 modules). Awaiting go to start **Wave 1**:
  [1] output · [5] backend-determinism · [4] spec-gate-critic · [2] prose · [14] compose.
- **Wave 1 STARTED** — 5 lanes launched as worktree-isolated agents (branches
  `wave1/{output,prose,spec-gate,determinism,compose}`). Gitignored `.claude/`.
- **[1] output dir — DONE.** Critic CLEAN (no findings); merged to branch; full
  suite green. `run(output_dir=)` / `--out DIR` writes the gate-reviewed tree.
- **[5] backend determinism — DONE.** Critic CLEAN (2 non-blocking latent MINORs:
  `gpt-*` could over-match a hypothetical Ollama `gpt-oss`; legacy `claude-3-opus`
  id unmatched — both outside default tier maps, injectable escape hatch). Merged;
  full suite green. Injectable `SamplingPolicy` (effort vs temperature per model).
- **[14] compose profile — DONE.** Critic CLEAN (path-traversal defeated, drop-in
  parity confirmed). Merged. TESTED: `FilesystemArtifactStore` (durable drop-in) +
  `make_checkpointer` env switch. Docker/compose/docs are marked scaffolding
  (image digests = REPLACE_ME; isolated containers/observability = placeholders).
- **[2] prose bodies — DONE.** Critic CLEAN. Merged. `ProseBody` round-trips PRD/
  design/ADR narrative through the CAS under the same handle; docs inline it.
- **[4] spec-gate critic — DONE.** Critic CLEAN (trust verified: no wrapper-
  suppression path) + 2 MINORs fixed (blank-omission predicate alignment; tree-sha
  identity test). Merged. Critic-flagged security omission now FAILS the Spec gate.
- **WAVE 1 COMPLETE** — all 5 lanes merged, each critic-CLEAN, full suite green
  after every merge. NEXT: Wave 2 — [3] doc-node (needs [1]) · [6] CLI parser
  hardening · [11] agent journal. (Hold [7] drift for Wave 3: shares manifest.py
  with [3].)
- **Wave 2 STARTED** — 3 lanes off `cda4f55` (branches `wave2/{doc-node,cli-parsers,journal}`).
- **[6] CLI parser hardening — DONE.** Critic CLEAN. Merged; full suite green.
  Codex `item.completed`/`agent_message` nesting handled + raises on zero-exit
  empty (no silent empty completion); Claude robust to modelUsage-absent envelope,
  still fail-closed. Real-shape fixtures added. (Out-of-scope note for later:
  Claude doesn't raise on empty-result-text-with-observable-model.)
- **[11] agent journal — DONE.** ADVERSARIAL critic CLEAN — advisory-only
  guarantee holds structurally (zero reads of `agent_journal` into merge_gates /
  route_after_gates / admission / evidence-pack; `interview()` has no channel-write
  surface; reviewer_critic gate write unchanged; zero added model calls). Merged.
  Wired into product_manager/principal_architect/reviewer_critic (full rollout
  mechanical). Branch-name collision (work on `worktree-agent-aa8f2d99f3cb51476`)
  was harmless — merged the correct ref.
- **[3] doc-node — DONE.** Critic CLEAN. Merged. Doc assembly is a terminal `docs`
  graph node (PASS-path only, best-effort, budget-metered, artifacts on the
  checkpointed channel); `GateRef.attested` counts judgment bindings; cli writes
  the bundle via a per-run `doc_sink`. Trivial duplicate-import nit cleaned up.
- **WAVE 2 COMPLETE** — [3]/[6]/[11] merged, each critic-CLEAN, full suite green
  (1642+). 9/15 tasks done. NEXT: Wave 3 — [8] spec ingestion · [7] drift
  actionable. (Then Wave 4: [9] modes + [13] ideation module; Wave 5: [10]
  checkpoints; Wave 6: [12] evolution.)
- **[7] drift actionable — DONE.** Critic CLEAN (trust verified). Merged. Injectable
  DriftPolicy: advisory default, opt-in fail-closed raises on gate/judgment drift
  before stamping; tier-mismatch surfaced.
- **[8] spec ingestion — DONE.** Critic CLEAN (no gate/criteria path reads
  `state['spec']`) + 1 MINOR fixed (empty-string `_load_spec("")` no longer ingests
  cwd). Merged. `--spec FILE|DIR` / `run(spec=)` seeds the spec into the product
  intake prompts; request-only byte-identical.
- **WAVE 3 COMPLETE** — [7]/[8] merged, critic-CLEAN, full suite green. 11/15 done.
  NEXT: Wave 4 — [9] `--mode`/`--packs` + [13] ideation module (both unblocked by
  [8]). Then Wave 5: [10] checkpoints; Wave 6: [12] evolution. ([15] worker pool
  deferred to a final infra pass after [14].)
- **BRANCH RECOVERY (between Wave 3 and 4):** the main worktree had drifted onto
  `main`@3d6fe84 while the feature branch ref lagged at cda4f55 — all Wave 2-3
  merges were reachable (linear at 8b6299d) but not on the feature ref, and pushes
  were no-ops. Fixed: checked out `claude/...-43wylm`, `--ff-only 8b6299d` (clean,
  cda4f55 is an ancestor), pushed cda4f55..8b6299d. No work lost; nothing wrongly
  pushed to `main`. All subsequent work is on the feature branch.
- **[9] `--mode`/`--packs` — DONE.** Adversarial critic CLEAN (assistant default
  byte-identical; pack veto fail-closed + emit-boundary defense-in-depth; autonomous
  admission genuinely re-verifies; assurance gates NOT faked; HITL preserved). Merged.
- **[13] ideation — DONE.** Critic CLEAN (advisory, no channel-write surface,
  bounded spend) + 1 MINOR fixed (standalone entry now degrades gracefully). Merged.
- **WAVE 4 COMPLETE** — [9]/[13] merged, critic-CLEAN, full suite green. 13/15 done.
  Gated/Autonomous + domain packs now reachable from the CLI; ideation front-end
  ships a spec consumable by `--spec`. NEXT: Wave 5 — [10] checkpoint/HITL mode
  (+ the small [13] cli `--ideate` handoff). Then Wave 6: [12] evolution route.
