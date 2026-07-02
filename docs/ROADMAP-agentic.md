# Roadmap: from gated slices to large autonomous builds

Goal: Foundry should be able to build a large, complex, iteratively-grown
project — the benchmark target is a **full Stardew-style 2D game** (graphics,
gameplay systems, economy, save/load) — either gated or autonomously, with good
results, on **either** a Max subscription (`claude -p`, no API billing) **or**
the metered Anthropic API. The choice of billing path must be a config swap,
never a code change.

## Where the ceiling is today (assessment, 2026-07)

The governance side (gates, budgets, provenance, durability, `evolve`) is
strong. The generation side is deliberately thin, and three things cap it:

1. **One completion per task.** `raw-react` sends one prompt and expects one
   unified diff back (`foundry/execution/adapters/raw_react.py` — `turns_used=1`
   is hardcoded). No inner write→build→test→fix loop. Even the `claude-cli`
   backend invokes `claude -p` as a bare completion, not an agentic session.
2. **Additions-only integration.** `gate_wiring._parse_addition_diff` fails
   closed on any edit/delete/rename, and each task runs in a fresh sandbox that
   never sees sibling code. Nothing can grow a shared codebase.
3. **Python-only verification.** Build = `compileall`, test = `pytest`. The
   planner already targets games (`platform/planner.py` infers LÖVE / Godot /
   pygame / Phaser and demands a full game loop), but a generated game is never
   built, launched, or exercised — and the codegen prompt is starved (`spec_uri`
   unresolved, `retrieved_spans` empty).

The phases below attack these in leverage order. Phases 1–2 change what the
factory *can write*; Phase 3 changes what "green" *means*; Phases 4–6 change
how far it can *go alone*.

---

## Phase 1 — Agentic coding adapter, dual billing path

**The single biggest lever.** Add a `claude-code` adapter behind the existing
`WorkerAdapter` seam (`execution/adapter_contract.py` names Claude Code as an
intended substrate; the protocol already fits).

### 1a. `ClaudeCodeAdapter` (`foundry/execution/adapters/claude_code.py`)

- Runs the `claude` CLI **agentically inside the task sandbox worktree**:
  `claude -p "<assembled prompt>" --output-format json` with
  `cwd=worktree`, `--permission-mode acceptEdits`,
  `--allowedTools "Read,Write,Edit,Glob,Grep,Bash(<pinned build/test cmds>)"`,
  and `--max-turns` mapped from `packet.turn_budget`. Claude Code edits files,
  runs the build/tests, and iterates — the inner loop the contract says
  "lives in the substrate."
- Prompt: reuse `assemble_prompt()` unchanged (pinned constraints stay first
  and verbatim; `prior_failures` keeps powering the gate fix loop).
- **Result extraction:** unlike `raw-react`, the adapter mutates the worktree.
  Snapshot the tree before and after (`Sandbox.snapshot()` both sides), compute
  the unified diff between the two trees, store it as the content-addressed
  `diff_uri`. `turns_used` ← envelope `num_turns`; observed model ←
  `modelUsage` (same logic as `ClaudeCliTransport._observe_claude_model`);
  `total_cost_usd` ← envelope (0/nominal on subscription auth).
- `available()` probes the binary; raise `AdapterUnavailableError` otherwise.

### 1b. Billing paths — `-p` (Max) and API, same adapter

The `claude` binary already supports both credentials, so **one adapter covers
both paths**; the difference is only what auth reaches the subprocess:

| Path | Auth | Billing | Config |
|---|---|---|---|
| Max subscription | machine login session, or `CLAUDE_CODE_OAUTH_TOKEN` in `.env` | none (plan usage) | `FOUNDRY_MODEL_BACKEND=claude-cli` (default in the Mac/Linux presets) |
| Metered API | `ANTHROPIC_API_KEY` | per-token | same adapter; key present ⇒ CLI bills the API |

- **Sandbox credential grant (design decision, must be recorded):** the sandbox
  env-allowlist deliberately denies `ANTHROPIC_*`/`*_TOKEN` and redirects
  `HOME`. An agentic `claude` inside the cage needs its auth. Add an explicit,
  adapter-scoped grant: the adapter passes a `credential_env` /
  `home_passthrough` extension to `Sandbox` that admits **only** the chosen
  auth variable (or the real `HOME` for a cached login session), nothing else.
  This moves the credential locus into the cage for this adapter — the same
  acknowledged trade-off `cli_transports.py` already documents for CLI
  backends ("the credential locus moves"). Network denial for the subprocess
  is correspondingly relaxed to "Anthropic egress only" (dev-grade: documented;
  production: netns allowlist).
- **Cost enforcement per path:** metered runs keep `cost_budget_microusd`
  enforced from envelope `total_cost_usd`. Subscription runs report ~0 cost, so
  the binding budgets become `turn_budget` + the sandbox wall-clock cap —
  enforce both, and record which regime was active in the evidence pack.

### 1c. Native API agentic adapter (keeps the trust story intact)

The CLI path trades away "proxy is the sole credential holder." For users who
want the metered API **with** the original trust posture, add
`anthropic-agent`: a tool-use loop (Read/Write/Edit/Bash tools implemented
against the `Sandbox`) where every model call routes through the existing
`ModelProviderProxy` — proxy keeps lineage observation and stays the only
secret holder. This is more work than 1a and can land second; 1a unblocks
everything.

### 1d. Selection & wiring

- `--adapter claude-code | anthropic-agent | raw-react | stub`;
  `FOUNDRY_ADAPTER` env twin. Derivation: `claude-cli` backend ⇒ `claude-code`
  default; `anthropic` backend ⇒ `anthropic-agent` once it exists (until then
  `raw-react`).
- Update both `foundry.*.env.example` presets and README §quick-start.

**Exit criteria:** a single task on the `claude-cli` backend produces a
multi-file, build-passing change with `turns_used > 1`, on both auth paths,
with zero API spend on the subscription path.

---

## Phase 2 — Full patch application + shared evolving tree

Without this, Phase 1 quality is wasted — tasks still couldn't touch shared
modules.

- Replace `_parse_addition_diff` with a real patch applier (edits, deletes,
  renames; still fail-closed on malformed hunks). Since the agentic adapter
  produces a *tree*, integration may equivalently consume the post-run tree
  snapshot directly and diff against the task's base — pick one canonical
  path and keep the addition-diff parser as the `raw-react` fallback.
- Give each task its base tree = **the current merged project tree**, not the
  empty scaffold. Scheduling: topological waves by ownership-map dependency
  (parallel within a wave, merge between waves); replace the stubbed conflict
  resolution in `senior_engineer.py` with real three-way merge + a re-gate on
  conflict.
- `evolve` inherits full-patch materialization for free (it shares the
  applier).

**Exit criteria:** two sequential tasks where the second edits a file the
first created, and the merged tree passes gates.

---

## Phase 3 — Stack-appropriate gates ("green" must mean "it runs")

The two-zone runner already takes pinned commands; add per-stack `GateImpl`s
selected from the planner's engine inference:

- **web/React:** `npm ci && npm run build` (build gate), `npm test`
  (test gate), Playwright + bundled Chromium smoke (E2E).
- **Phaser/HTML5 game:** build, then headless-Chromium boot: page loads, game
  canvas renders, **N simulated ticks/inputs without an uncaught exception**,
  screenshot captured as evidence-pack artifact.
- **pygame:** `SDL_VIDEODRIVER=dummy` boot-and-run-N-frames gate; pytest for
  logic.
- **LÖVE/Godot:** gate on toolchain presence (`love .` / `godot --headless`);
  fail closed with a clear "toolchain missing" ERROR, never a silent pass.
- Keep pytest/compileall as the Python default. Gate selection = pinned at
  architecture time from the plan's engine choice, recorded in the manifest.

**Exit criteria:** a generated Phaser game that throws on frame 1 **fails**
the E2E gate; one that runs 500 ticks passes, with a screenshot in the
evidence pack.

---

## Phase 4 — Feed the coder real context

Cheap, large quality win, independent of the adapter:

- Resolve `spec_uri` bodies (CAS fetch) into the packet instead of shipping a
  bare handle; populate `retrieved_spans` with the relevant PRD criteria +
  design sections for the task's criterion_ids (simple keyword/criterion-id
  retrieval first; embeddings later if needed).
- Include the current tree's file listing + public interfaces of the task's
  dependencies (from the ownership map) so tasks compose instead of guessing.
- Raise or chunk the 24k-char spec truncation in the product stage.

---

## Phase 5 — Milestone loop: autonomous long-horizon growth

Today the only "keep going" mechanism is the deepening loop (≤6 passes) and
`evolve` re-gates but never authors. Add the authoring loop:

- **Milestone planner:** architect emits an ordered milestone list for big
  targets (game example: engine loop → world/tilemap → farming → NPCs/dialog →
  economy → save/load → polish), each milestone = criteria + touched areas.
- **Milestone executor:** for milestone k>1, drive `evolve` with an
  LLM-authored change set produced by the Phase-1 adapter against the released
  tree; prior criteria/oracles ride along as the regression suite (already
  built). Guardian/HITL posture per `--mode` as today.
- Completion: the platform critic names gaps per milestone; plateau detector +
  per-milestone budget caps bound each cycle; `--state-dir` durability makes
  the whole build resumable across days (already works — this phase just gives
  it something to resume *toward*).
- Workshop UI: a milestone track per floor — each milestone is a release with
  lineage, so the existing iterate/evidence flow applies unchanged.

**Exit criteria:** a two-milestone build where milestone 2 extends milestone
1's released tree unattended in `autonomous` mode, with regression suite green.

---

## Phase 6 — Assets & a `game` domain pack

- **Assets first as curation, not generation:** a pinned CC0 asset-pack step
  (sprites/tilesets/audio) fetched at build time with digests recorded in the
  manifest; generation (sprite synthesis) later, behind the same seam.
- **`game` domain pack** (`registry.register()` + `_PACK_FACTORIES`): packs
  can only tighten, so it contributes governance — mandatory playability HITL
  (modeled on the frontend pack's perceptual HITL), a required runtime-gate
  key, and a mode veto for `autonomous` until Phase 3 gates exist for the
  chosen engine. Capability itself lives in Phases 1–3, not the pack.

---

## Sequencing & effort

| Phase | Depends on | Size | Payoff |
|---|---|---|---|
| 1 agentic adapter (dual path) | — | M | biggest single quality jump |
| 2 full patches + shared tree | 1 | M | unlocks any multi-module project |
| 3 stack gates | — (parallel w/ 1–2) | M | "green" becomes meaningful for games |
| 4 context enrichment | — (parallel) | S | quality per call |
| 5 milestone loop | 1–3 | L | true long-horizon autonomy |
| 6 assets + game pack | 3 | M | playable, not just runnable |

Recommended start: **1a + 1b**, with **3 (Phaser gate)** and **4** in
parallel; then 2; then 5. The Stardew-style benchmark becomes runnable
end-to-end after Phase 5, playable after Phase 6.
