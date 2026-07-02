# Roadmap: from gated slices to large autonomous builds

> **Status (2026-07): ALL SIX PHASES SHIPPED.**
> 1a/1b/1d — `ClaudeCodeAdapter` (agentic `claude` in-sandbox), typed
> credential grant (model-auth only; `AWS_*` never grantable), dual Max/API
> billing, `claude-cli` → `claude-code` derivation. 1c — `ProxyAgentAdapter`
> (multi-turn ReAct through the mediating proxy; trust story intact, any
> backend). 2 — full patch application (writes/deletes/context-verified
> patches) + the serial shared evolving tree (each task builds on its
> predecessors' slices). 3 — verification profiles (python / node-web /
> phaser-web / pygame / love2d / crypto / iac-terraform) + `SmokeGate`
> runtime boot + smoke-aware boundary; `run(profile=...)`. 4 — criteria/
> design/unit spans in the codegen packet. 5 — `foundry milestones`
> subcommand: planned milestone list, full factory per milestone, released
> trees chained via `base_dir`, per-milestone resume. 6 — `game`/`crypto`/
> `infra` tighten-only packs (playability HITL, gated-only crypto release,
> plan-review HITL + no hands-off deploy).
> Remaining (deliberately): pooled-path sibling visibility (parallel waves +
> real conflict resolution), asset generation/curation, browser-real Phaser
> smoke (Playwright), and rung-by-rung benchmark validation (the ladder
> below).

Goal: Foundry should be able to build a large, complex, iteratively-grown
project — **any** domain: the benchmark target is a full Stardew-style 2D game
(graphics, gameplay systems, economy, save/load), but the same pipeline must
carry encryption software, AWS infrastructure, and other complex systems —
either gated or autonomously, with good results, on **either** a Max
subscription (`claude -p`, no API billing) **or** the metered Anthropic API.
The choice of billing path must be a config swap, never a code change.
Domain-specificity is confined to two seams: the **verification profile**
(Phase 3) and the **domain pack** (Phase 6); Phases 1–2 and 4–5 are
domain-agnostic by construction.

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
- **Credential typology — the grant is typed, and only one type ever enters
  the cage.** Generalizing beyond games (crypto software, AWS infra) forces a
  distinction the mechanism must encode:
  - *Model-auth grant* (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` /
    `HOME` passthrough): the ONLY grant admissible during the
    untrusted-execution phase. Blast radius = model spend/quota.
  - *Target-system credentials* (`AWS_*`, deploy keys, kubeconfigs, signing
    keys): **never enter the sandbox**, ever. Generated code must not be able
    to touch real infrastructure. These belong exclusively to the
    **credentialed phase** the architecture already defines (`sandbox.py`
    docstring: the sandbox is destroyed and rebuilt between the untrusted
    phase and any credentialed phase) — i.e. `terraform apply`/deploy runs as
    a trusted post-gate step, driven by Foundry itself, only after gates pass
    and per the mode posture (`assistant`/`gated` = human sign-off before
    apply; `autonomous` hands-off deploy only where a pack doesn't veto it).
    The deny patterns already block `AWS_*` — that stays absolute; every
    grant issued is stamped with its type in the evidence pack.
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

## Phase 3 — Verification profiles ("green" must mean "it works")

Domain-neutral framing: the two-zone runner already takes pinned commands, so
gates become **verification profiles** — a named bundle of build/test/E2E/
security `GateImpl`s selected from the planner's stack inference, pinned at
architecture time, recorded in the manifest. Games are just one profile; the
same seam carries any complex software.

- **web/React:** `npm ci && npm run build` (build gate), `npm test`
  (test gate), Playwright + bundled Chromium smoke (E2E).
- **Phaser/HTML5 game:** build, then headless-Chromium boot: page loads, game
  canvas renders, **N simulated ticks/inputs without an uncaught exception**,
  screenshot captured as evidence-pack artifact.
- **pygame:** `SDL_VIDEODRIVER=dummy` boot-and-run-N-frames gate; pytest for
  logic.
- **LÖVE/Godot:** gate on toolchain presence (`love .` / `godot --headless`);
  fail closed with a clear "toolchain missing" ERROR, never a silent pass.
- **crypto/encryption software:** policy gate — generated code MUST use vetted
  primitives (`cryptography`, libsodium, age); hand-rolled ciphers/modes and
  weak algorithms (ECB, MD5/SHA-1-for-auth, static IVs) fail closed via
  pattern/AST rules in an extended SecurityGate. Correctness gates: known-
  answer test vectors (NIST/RFC KATs) for every primitive touched, plus
  property-based round-trip and tamper-detection tests (encrypt→decrypt
  identity; modified ciphertext MUST fail authentication). Deterministic and
  perfectly suited to the two-zone runner.
- **AWS/IaC (Terraform, CDK, CloudFormation):** build gate =
  `terraform fmt -check && terraform validate` (or `cdk synth`); security
  gate = policy-as-code scan (checkov/tfsec/OPA — public S3, wildcard IAM,
  unencrypted stores fail closed); E2E gate = `terraform plan` against
  LocalStack or a sandbox account, asserting the plan applies cleanly and
  matches declared resources. **`terraform apply` is NOT a gate** — deploy is
  a post-gate, credentialed-phase action governed by mode (see below).
- Keep pytest/compileall as the Python default profile.

**Exit criteria:** a generated Phaser game that throws on frame 1 **fails**
the E2E gate; one that runs 500 ticks passes, with a screenshot in the
evidence pack. A generated crypto module with a hardcoded IV fails the policy
gate; a Terraform stack with a public S3 bucket fails the policy scan.

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

## Phase 6 — Assets & domain packs (game, crypto, infra)

- **Assets first as curation, not generation:** a pinned CC0 asset-pack step
  (sprites/tilesets/audio) fetched at build time with digests recorded in the
  manifest; generation (sprite synthesis) later, behind the same seam.
- **Domain packs** (`registry.register()` + `_PACK_FACTORIES`): packs can only
  tighten, so each contributes governance; capability lives in Phases 1–3.
  - `game`: mandatory playability HITL (modeled on the frontend pack's
    perceptual HITL), a required runtime-gate key, mode veto for `autonomous`
    until Phase 3 gates exist for the chosen engine.
  - `crypto`: requires the crypto verification profile (KAT + policy gates),
    security-specialist approver-of-record, mode veto on `autonomous` release
    of anything exporting a cryptographic API — human sign-off always.
  - `infra`: requires the IaC profile, plan-artifact HITL before any apply,
    approver-of-record for production targets, egress profile pinning deploys
    to the credentialed phase (see Phase 1b credential typology). The existing
    `RegulatedPack` is the template for all three.

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

---

## Target portfolio & benchmark ladder

The factory is built ONCE (Phases 1–5); breadth comes from adding
verification profiles + packs (Phases 3/6), one per rung. Each rung below is
a real project the factory must build end-to-end before claiming the rung —
ordered so every rung adds exactly one new capability, and earlier rungs
become regression benchmarks for factory changes (their conformance suites
give an objective quality score for tuning the factory itself).

| # | Benchmark project | New capability proven | Profile / pack |
|---|---|---|---|
| 1 | CHIP-8 emulator (public test ROMs) | agentic adapter + multi-file edits (Phases 1–2) | `python` default |
| 2 | Small DSL / transpiler w/ conformance suite | milestone loop on a pure-logic target (Phase 5) | `python` default |
| 3 | CLI dev tool (log analyzer / migration tool) | fixture-execution gates | `cli` |
| 4 | REST API service + ephemeral DB | contract tests, integration E2E | `web-service` (planner's SaaS spine) |
| 5 | ETL pipeline w/ golden datasets | schema/idempotency gates | `data` + `ml_data` pack |
| 6 | Constraint solver (roster/timetable) | invariant + known-optimal gates | `python` default |
| 7 | Text roguelike | game-state-machine gates, save/load | `game` (text tier) |
| 8 | **Stardew-style moon-colony game (Phaser)** | headless-browser runtime gates, assets, playability HITL | `game` + Phase 6 assets |
| 9 | File-encryption tool / password vault | KAT + crypto-policy gates, crypto pack veto | `crypto` |
| 10 | Terraform AWS stack (multi-service) | plan/policy gates, credentialed-phase deploy | `infra` |
| 11 | Rich-UI web app (dashboard/notes) | screenshot-diff + perceptual HITL at scale | `web-ui` + frontend pack |
| 12 | Foundry writes a new verification profile for itself | self-extension under guardian approval | `regulated`-style gating |

Rungs 1–2 are the Phase-1/2/5 acceptance tests. Rungs 3–7 are cheap
(profiles reuse the two-zone runner; mostly pinned-command bundles). Rung 8
is the flagship. Rungs 9–10 are governance-heavy (packs + credential
typology). Rung 12 is deliberately last: the factory extending its own gates
is the highest-leverage and highest-trust step, and must ride on a mature
guardian/evidence flow.
