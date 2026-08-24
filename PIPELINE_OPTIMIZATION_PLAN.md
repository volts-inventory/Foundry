# Pipeline Token & Latency Optimization Plan

Status: **decided, not yet implemented.** A selected approach is recorded for
every item in §3 "Decisions." The proxy is **kept**; only the coding adapter is
swapped (`proxy-agent` → `claude-code`, B1), which deletes the quadratic and
keeps the gate trust model intact (B3 dropped). Each item lists its own
verification and blast radius; the high-blast-radius items (D1, D7, B2) are
marked for Plan Mode + a second-opinion pass before any code lands.

## 1. Problem

Runs burn through token/usage limits and are slow. This is not a LangChain
`ChatModel` problem — there is no `ChatAnthropic`/`bind_tools` anywhere.
LangGraph only orchestrates; every model call funnels through one hand-rolled
egress (`ModelProviderProxy.complete/judge`). The proxy itself is sound (single
credential, bounded retries, prompt-cache breakpoint, per-call usage metering).
The cost lives in **what the callers feed it** and **how many serial calls a run
makes**.

## 2. Diagnosis (what we found)

Two independent problems compound:

**Token burn**
- The `proxy-agent` coding adapter re-sends a **growing transcript** every turn
  (assistant turns + full `OBSERVATION:` file dumps), up to `turn_budget=8`
  turns/task, at **T1/Opus** with **adaptive thinking on** — quadratic, on the
  highest-volume path. (`foundry/execution/adapters/proxy_agent.py:127-152`,
  tier/purpose hardcoded at `foundry/cli.py:231` and `:258`.)
- Flat `max_tokens=16000` on **every** call regardless of purpose.
  (`foundry/proxy/model_provider_proxy.py:122`, applied `:959`.)
- Prompt caching covers only the system SOP and has a 5-min TTL; rate-limit
  waits (up to 6h, `model_provider_proxy.py:676`) go cold, so "cached" SOPs
  re-bill at full input price after a wait. Non-SDK transports (Ollama/OpenAI
  HTTP) drop `cache_control` entirely.

**Latency / structure**
- The spine is **almost entirely serial**: ~18-20 sequential model calls (several
  T1) on the happy path before a single gate PASS. Each subgraph is one blocking
  supervisor node.
- The three implementation lanes (frontend/backend/data) run **serially** as a
  conditional chain, each waiting on the coding substrate — the heaviest stage —
  even though lanes own disjoint files.
  (`foundry/graph/subgraphs/implementation.py:160-186`.)
- Fix loops re-run whole layers up to `max_fix_attempts` (default 3, a single
  **global** budget never reset in-graph). One build/test fix re-runs up to 6
  engineer nodes + the coding substrate again.
- **Redundant work:** `reviewer_critic` runs twice (quality **and** assurance,
  same factory) reviewing the same integrated diff; `sdet` E2E + the assurance
  E2E gate both exercise E2E.
- **Dead outputs:** `stakeholder_proxy`'s `IntentBrief` and `eng_program_mgr`'s
  `SprintPlan` appear to be consumed by no downstream agent or gate; the
  Principal Architect doesn't even read the PRD prose body — only
  `AcceptanceCriterion[]` and `required_capabilities` flow forward from the
  5-node product layer. (Verify §6 before cutting.)
- **The reviewer reviews blind:** `reviewer_critic` is sent only the subject
  *URI*, no diff body (`foundry/agents/quality/reviewer_critic.py:166`), to a
  model with no filesystem — so its verdict is uninformed, and a spurious
  `request-changes` drives the expensive fix loop.

## 3. Proposed changes

Grouped into four workstreams. Impact = expected token/latency reduction;
Blast = reversibility/policy/trust risk.

### Workstream A — Token cost (low blast, do first)

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| A1 | Stop hardcoding the coding adapter at `T1` + `purpose="ideation"`. Route most tasks to T2/T3; reserve T1 for the last fix attempt. Drop `ideation` (kills adaptive-thinking tokens on the highest-volume path). | `cli.py:229-265` | High | Low |
| A2 | **De-quadratic-ize the `proxy-agent` transcript** — it re-sends the whole accumulated transcript (assistant turns + full `OBSERVATION` file dumps) every turn, uncached past the SOP, so cost is O(turns²). Three composable fixes: (a) **bound the window** — sliding window / summarize-or-drop old observations (attacks count + latency, O(n²)→O(n·window)); (b) **incremental prompt-cache breakpoints** on the message prefix so re-sent history is a cache read, not a re-bill (attacks cost → near-linear; TTL-sensitive); (c) **lower `turn_budget`** from 8 (shrinks n). Also stop putting whole file bodies in observations — return references/deltas. Preferring the `claude-code` adapter (B1) moots all of this — the CLI manages its own context with native caching. | `proxy_agent.py:127-152`; today only the system block carries `cache_control` (`model_provider_proxy.py:960`); `junior_engineer.py:98` | High | Low |
| A3 | Per-**purpose** `max_tokens` instead of a flat 16k. Verdicts/structured JSON ~1-2k; decomposition ~2-4k; only free-form ideation needs a large ceiling. | `model_provider_proxy.py:122,959` (add a purpose→cap map in `_build_request`) | Medium | Low |
| A4 | Lower `max_fix_attempts` default from the CLI wrapper (5) toward the graph default (3), and wire the existing `cost_cap_microusd` into an early stop so a run halts on spend, not just on attempt count. | `cli.py:1288/2187`; `junior_engineer.py` pool path already has the ledger | Medium | Medium |

### Workstream B — Claude Code CLI integration (medium blast)

Context: the `claude-code` adapter is already a long-lived per-task process that
runs its own internal agentic turns with native caching — the efficient shape.
The `proxy-agent` adapter re-implements the loop externally (the A2 hog). See §5
for why a persistent session **per reasoning agent** is explicitly rejected.

**Decision (B1): keep the proxy; nix the `proxy-agent` adapter.** The proxy and
the `proxy-agent` adapter are different things: the proxy is the shared model
egress used by all ~16 reasoning/judge agents; `proxy-agent` is ONE coding
adapter that re-implements a ReAct loop on top of it (the quadratic). Keep the
proxy exactly as-is — it goes on signing verdicts (gates stay un-forgeable),
metering, lineage/drift, rate-limit handling, and holding the single credential —
and only swap the coding substrate from `proxy-agent` to **`claude-code`** (one
`claude -p` invocation per task, internal turns, CLI-managed context + native
caching). That deletes the quadratic without touching anything else, so **A2 and
C3 become moot** (there is no external ReAct loop left to bound), and **B3
disappears** (the signer stays because the proxy stays — no advisory-gate risk).

One trade (inherent to `claude-code`): the coding call authenticates in the
sandbox cage and does NOT ride the proxy egress, so it is absent from the proxy's
token manifest — but the adapter already observes `total_cost_usd`/usage from the
CLI envelope, so cost stays visible. Wanting the coding call to *also* ride the
proxy is the only reason to instead keep `proxy-agent` and bound its context
(more work, mitigates rather than deletes the quadratic).

**The shared pool is untouched regardless.** The proxy is the *agent→model*
egress; the shared workspace where agents publish/read each other's work is a
separate thing — the LangGraph shared state channels (`artifacts`, `gates`,
`tasks`, …) plus the content-addressed artifact store (CAS), the MetaGPT
message-pool + subscription equivalent. proxy = agent→model; shared pool =
agent→agent. Neither this decision nor any other here changes the shared pool.

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| B1 | Default the coding substrate to `claude-code` (per-task process, native caching) over `proxy-agent`. Captures ~all of "long-lived CLI" for coding with no protocol work. Keep `proxy-agent` only where the mediating-proxy credential locus is a hard requirement. | `cli.py` adapter selection | High | Medium (trust: `claude-code` moves the credential into the sandbox cage — already a documented trade) |
| B2 | **Cross-fix session continuity** (the bidirectional stream-json idea, applied where it pays): on a fix attempt, `--resume <session_id>` the prior coding session instead of re-priming the whole task. The result envelope already exposes `session_id` (`cli_transports.py:366`). Directly attacks the fix-loop multiplier. | new adapter capability + fix-loop wiring | High | **High** — conflicts with destroy-per-task / reset-to-base idempotency; needs the worktree + `session_id` kept alive across the loop and stored durably for graph resume. Design doc + second opinion before building. |

### Workstream C — Context exposure per agent (low–medium blast)

We do **not** summarize context anywhere; exposure is limited only by ad-hoc
per-node truncation caps (spec 24k, span 4k, judge-evidence 800 chars). Two
miscalibrations point in opposite directions.

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| C1 | **Give the reviewer the actual diff.** Feed the changed hunks (scoped, capped) into `reviewer_critic`'s message instead of a bare URI. Fixes uninformed verdicts that drive the fix loop. This is *more* context, not less. | `reviewer_critic.py:158-177`; reuse the `_retrieved_spans` resolve pattern from `junior_engineer.py:253` | High (indirect: fewer bogus fix loops) | Low–Medium (correctness of a gate input — security-review the change) |
| C2 | Generalize `_ai_judge_evidence` (`factory_graph.py:404`) into a per-role **context contract**: an explicit "what this agent must see, capped, and nothing else" digest, replacing scattered magic-number caps. | new helper; adopt per node incrementally | Medium | Low |
| C3 | If `proxy-agent` is kept (B1 not universal): summarize/prune stale observations rather than re-sending file bodies (overlaps A2). | `proxy_agent.py` | Medium | Low |

### Workstream D — Pipeline redundancy & parallelism (medium–high blast)

| # | Change | Where | Impact | Blast |
|---|--------|-------|--------|-------|
| D1 | **Parallelize the three implementation lanes.** Convert the lead chain and the engineer chain from a conditional serial chain to concurrent fan-out (lanes own disjoint files; reducers are keyed unions built for concurrent writes). Biggest wall-clock win. | `implementation.py:127-186` | High (latency) | **High** — touches state reducers, determinism, resume, and the serial-overlay "shared evolving tree." Plan Mode + second opinion. |
| D2 | **Drop or scope the second `reviewer_critic`** (assurance). Either remove it, or scope it to reviewing *security-agent findings* the quality reviewer didn't see, or gate it behind "tree changed since the quality review." | `assurance.py:356,481,489`; check `assurance_gate` doesn't require a fresh `review` verdict first | Medium | Medium (verify the assurance gate's inputs — security-review) |
| D3 | **Cut the dead-output product nodes.** Pending the §6 consumer check: remove `stakeholder_proxy` (T2) and `eng_program_mgr` (T2), or downgrade to T3/no-LLM if only the UI/interview layer reads them. Removes 2 serial T2 calls/run. | `product.py:161-162,177` | Medium | Medium (verify no webapp/interview consumer) |
| D4 | **Fold `criteria_completeness_critic` into the PM prompt.** The producer/critic pair over one artifact costs a T1 call + up to 2 PM revision round-trips; the PM SOP can carry the completeness checklist directly. Keep the critic only if an independent second pass is genuinely required. | `product.py:163,172-176`; `product_manager.py` SOP | Medium | Medium |
| D5 | **Reframed (verified):** no duplicate E2E *execution* exists — `sdet` only authors a report (one model call), the assurance E2E gate re-runs pytest itself and reads nothing from sdet. The `E2EReport` is build-dead advisory (docs-only), so downgrade `sdet` **T2→T3** rather than "dedup". | `sdet.py`; `gates/e2e.py` | Low | Low |
| D6 | Make `docs` (T2, every PASS) cheaper (T3) or async — it's on the release critical path but not release-gating. | `factory_graph.py:1158-1166` | Low | Low |
| D7 | **Fan out the independent author chains.** Quality (`reviewer_critic`/`qa_engineer`/`sdet`) and assurance (`appsec`/`audit`/`supply_chain`) run as serial chains but the nodes are independent — they read the same integrated diff and don't consume each other's output. Run each subgraph's authors concurrently in one superstep (as architecture already does for domain_specialist + ux_design), converging at the gate. | `quality.py:112-115`; `assurance.py:486-490` | Medium (latency) | Medium — same reducer/`Seq` concurrency concerns as D1 (see track below), but no cross-file mutation. |
| D8 | **No action (verified).** `domain_specialist`/`ux_design` make **no model call** — they emit a deterministic `_addendum` marker `{matched_capabilities}` (`conditional.py:74-120`), zero tokens. So there is no token waste to recover and no rich design analysis to wire — they are placeholder scaffolding for future model-backed specialists, not a dead expensive output. Leave as cheap stubs; wiring the thin capability marker to the coder is low-value (the coder already has the design + criteria). Revisit only when these become real model-backed agents. | `conditional.py:74-120` | — | — |

### Dead/redundant verification (confirmed — full read-only producer→consumer trace)

Decisive fact: **the deterministic gates re-materialize an integrated tree from
`acceptance_criterion` + `feature_diff` and re-run everything themselves**
(`gate_wiring.materialize_workdir`, `assurance_wiring.materialize_assurance_workdir`);
they read **no** agent analysis artifact — the L5 trust model (agents propose,
gates re-derive + decide). So a whole class of agent output is **build-dead**: it
changes no build outcome, only the UI / checkpoint / docs narrative.

**Confirmed DEAD to the build** (consumed only by checkpoint display + webapp UI):
- `stakeholder_proxy` → `intent_brief` (the PM reads `request`/`spec`, not this) — D3.
- `eng_program_mgr` → `sprint_plan` + proposed cap (the ledger is seeded at triage) — D3.
- `appsec_engineer` → `threat_model`, `audit_compliance` → `audit_finding`,
  `supply_chain_reviewer` → `supply_chain_report` — the assurance gates re-scan the
  tree / re-ground the SBOM / police the diff and read none of these. **By design
  (trust model) — DO NOT cut the gates; downgrade the AGENTS for narrative (A5).**
- `qa_engineer` → `oracle_binding` — read only by the offline eval harness, not the
  build (negligible cost: a field of qa's existing output, not a separate call).

**Confirmed DEAD and read by nothing at all** (not even the UI): `domain_addendum`,
`ux_spec` — D8 (wire-or-cut).

**Confirmed REDUNDANT:**
- `reviewer_critic` at both quality + assurance on the SAME `feature_diff` (D2).
  **Router nuance:** the assurance verdict is load-bearing via the ROUTER, not the
  gate — `_route_assurance` fail-closes on a missing fresh review verdict
  (`assurance.py:139-158,194`). So the cut must ALSO change `_route_assurance`, else
  the boundary halts.
- E2E executed twice: `sdet`'s report + the assurance E2E gate re-running the
  scenarios (`gates/e2e.py`) — D5.

**Tiering implication (feeds A5):** an agent whose output is build-dead should run
at the cheapest tier that still yields readable narrative — its model quality gates
nothing. Starkest miss: `appsec_engineer` at T1 **Opus** on a threat model no gate
reads. Downgraded in A5.

**Removal policy (what actually gets DELETED vs kept):** "dead" ≠ "delete". The
only genuine deletions are the **redundant duplicates** (D2 assurance reviewer +
D5 double E2E). Everything else that is build-dead is KEPT because it still serves
the human/UI:
- Build-dead **advisory** (security agents, `intent_brief`, `sprint_plan`) →
  **downgrade + keep** — they populate the UI security room and the human
  checkpoint's release-review context (a reviewer wants the threat model/SBOM in
  front of them). Deleting them blinds the sign-off and empties the office.
- `domain_addendum` / `ux_spec` → **wire (fix)**, not delete (D8) — make them
  load-bearing rather than throwing the analysis away.
- `oracle_binding` → **leave** — a free byproduct of qa's output; the offline eval
  harness reads it.

Net: de-dupe + downgrade + wire. "Remove all dead" would strip real human-review
and UI value; only the two duplicates are actually removed.

**Why not just delete the build-dead advisory agents?** Because downgrading already
recovers ~all the cost (a Haiku/Sonnet call is near-free), so deleting saves only a
sliver *more* while blanking the human checkpoint review and emptying the UI. Delete
buys a rounding error and a regression; downgrade buys the savings with no loss.
**The exception — mode-gate them:** in a **headless autonomous run** (no human
checkpoints, no UI viewer) nothing reads the advisory outputs at all, so **skip the
advisory agents entirely in that mode** and run them (cheaply) only when a human or
the UI is present. That beats a blanket delete: full value when observed, zero cost
when not.

### Process-latency track (structural, rate-limit-aware)

This is the ordered answer to "make the *process* faster" (independent of
rate-limit stalls). Latency here is the serial waterfall + the coding substrate
run 3× back-to-back, not waiting on a throttle. Do it in this order:

1. **Shorten the chain (low blast, no concurrency):** C1 (reviewer sees the diff
   → fewer whole-layer fix re-runs — often the biggest *effective* speedup), then
   D2 / D3 / D4 / D6 to drop ~4 serial hops. None of these touch concurrency, so
   they carry no rate-limit interaction.
2. **Fan out independent authors (D7):** quality + assurance chains → concurrent.
   Medium blast.
3. **Parallelize the implementation lanes (D1):** the single biggest wall-clock
   win, high blast — design-first.

Concurrency mechanics (apply to D1 and D7): LangGraph runs parallel branches of a
superstep on a thread pool, so before ANY fan-out lands, two shared, non-thread-
safe seams must be fixed or determinism/resume breaks:

- **`EpochCounter.bump` is an unlocked read-modify-write on a shared dict**
  (`state/seq.py:46-50`). Concurrent nodes minting a `Seq` can race the
  `emission_epoch`, which is what makes a re-executed node outrank the stale
  record it supersedes — a corrupted epoch corrupts resume ordering. Make it
  lock-guarded (or per-node counters reconciled deterministically) as a
  prerequisite.
- **Channel reducers must tolerate concurrent writes.** The keyed unions
  (`union_by_id`, `merge_by_uri`, `union_journal`, `_merge_gates`) already merge
  independent writes, but confirm each is order-independent under a parallel
  superstep (the merge result must not depend on branch arrival order).
- **The serial "overlay" (shared evolving tree) in the junior path assumes
  ordered predecessor slices.** Parallel lanes lose cross-lane visibility mid-
  flight; disjoint ownership makes that moot, but the integration boundary must
  own the merge (it already does). Verify no lane relies on seeing another lane's
  in-flight slice.

**Keep rate-limiting in mind (the interaction):** parallel fan-out sends
concurrent requests, so on a constrained subscription it trips 429s *faster* —
turning a structural speedup into a throttle stall. So D1/D7 MUST ship with the
proxy's client-side pacing turned on (`max_rpm` / `max_concurrent_calls`, the
`_RateLimiter` seam, `model_provider_proxy.py:570-609`, off by default). Size
`max_concurrent` to the fan-out width (e.g. 3 lanes) and `max_rpm` under the
plan's ceiling. This is a pairing requirement, not optional: parallelism without
pacing converts the process win into a rate-limit loss.

### Workstream E — UI: one-to-one agent representation (webapp, low blast, UI-only)

Requirement: the "virtual office" floor shows **exactly the agents that run in a
given pipeline run — one unique character + desk per live agent instance — and
nothing else**. No cosmetic filler, no shared/dynamic pools, no invisible agents,
no permanently-staffed desks for agents that didn't run.

Today the floor is a Phaser 2D office (`webapp/static/game.js`) driven by an
event stream (`webapp/events.py`). Desks come from a static per-room `ROSTER`
(`game.js:141-148`); engineering is a shared Tech-Lead + dynamic IC *pool*
(`layoutEngineering`, `game.js:1844-1901`); several agents are invisible or
cosmetic (see §2 diagnosis).

| # | Change | Where | Notes |
|---|--------|-------|-------|
| E1 | **Dynamic staffing for every room.** Replace the static `ROSTER` with desks spawned from live telemetry — one unique desk+sprite per agent that actually runs, in its room; agents that don't run get no desk. Generalize the per-lane `layoutEngineering` pattern to all rooms so the roster tracks the live node set (and auto-follows any agent cut/added by Workstream D — no manual roster edits). | `game.js` `ROSTER`/`restaff`/`layout*`; `events.py` `live_event_for_node` | Floor size becomes a function of the live run. |
| E2 | **Give the invisible LLM agents desks.** `scope_judge` (add to `AGENT_BY_NODE`, product room — it currently returns `None` from `live_event_for_node`); `ux_design` + `domain_specialist` (move from `_INFRA_NODE` to `AGENT_BY_NODE`, architecture room, so they seat a sprite when they run instead of only a log line). | `events.py:29-52,170-174` | Conditional agents seat only when they actually run — consistent with "alive only." |
| E3 | **One desk per live IC.** Each `junior_engineer` IC spawned at runtime gets its own unique desk+sprite, and each lane Tech-Lead (`senior_engineer` per lane) its own — the floor grows/shrinks with the live IC count rather than a shared pool. | `game.js` `layoutEngineering:1844-1901`, `restaff:1549-1577` | Needs per-lane/per-IC identity in telemetry; the legacy `senior_engineer`/`junior_engineer` nodes carry no area and light all three lanes together (`events.py:40-44`) — the per-lane nodes must be the telemetry source, and IC identity must be surfaced per spawned worker. |
| E4 | **Seat the Technical Writer live** (or drop its desk). The `docs` node is agent-less infra today (`events.py:164`), so the Writer desk never seats even though it owns the doc artifacts. Emit live telemetry for `docs` so the Writer is a real seated agent. | `events.py:164`; docs node | Pick one: promote `docs` to a seated agent, or remove the Writer desk (it wouldn't be "alive"). |
| E5 | **Remove all cosmetic characters** — gate guard, idle pool runners, and the CTO. | `game.js` gate room `1017-1064`; `sendCTO`/`ctoPatrol` `1326`; checkpoint `1373`, `2162-2177` | **Open UX item:** the CTO is currently the *human-approver avatar* at checkpoints (spotlight + approve/changes/reject). Removing it needs a replacement approval affordance — a checkpoint modal/prompt with no character. Must be designed before E5 lands, or checkpoint approvals lose their UI. |
| E6 | **Unique appearance per agent.** Assign a distinct sprite/tint per agent role and a distinct variation per IC so no two desks share a look. | `game.js` `ROSTER`/character sheet `206-217` | The route1rodent sheet may lack enough distinct frames for a dynamic IC count — likely needs procedural tinting/variation to keep every worker unique. |

Design principle tying E to D: wire the on-screen roster off the **live node
set**, not a hardcoded list. Then Workstream D's cuts (drop Stakeholder Proxy /
Eng PM, remove the second Reviewer) reflect on the floor automatically, and the
UI never needs a lockstep edit when the pipeline changes. Infra/no-LLM nodes
(triage, gates, fix passes, sign-offs) stay deskless — they light a room / add a
log line only, which is consistent with "only agents alive."

Blast radius: UI-only, no pipeline logic touched. The single real design
decision is E5's CTO-replacement approval affordance.

### Decisions — selected approach for every item

Authoritative selection per item (supersedes any "either/or" phrasing in the
tables above). One decision needs an explicit product confirmation — **B1**,
flagged below; everything else is decided.

| Item | Decision | Selected approach / parameters |
|------|----------|--------------------------------|
| A1 | **N/A for claude-cli + HOLD the planner** | On the claude-cli backend the coder is ALREADY `claude-code` (default) — no `proxy-agent`/`raw-react` T1/ideation hardcode is in the path, so there is nothing to change there. The per-task **Opus plan micro-step** ADDS an Opus call per task (the coder runs on `claude-code`'s own CLI model, not our tiers), so like A5b it is a quality option that *increases* cost — HOLD for a cost-min user. Original intent kept for reference: short Opus plan → Sonnet `claude-code` do → Opus on the last fix attempt. |
| A2 | **Moot under B1** | B1 deletes `proxy-agent` entirely, so there is no external transcript to bound. `claude-code` manages its own context. |
| A3 | **Adopt** | Per-purpose `max_tokens`: gate/judgment/structured-verdict **2k**, decomposition/structured **4k**, free-form ideation **8k** (down from a flat 16k). |
| A4 | **Adopt** | CLI `max_fix_attempts` default **5→3** (match the graph); wire `cost_cap_microusd` as an early stop. |
| A5 | **Adopt — tier policy + fixes** | **Principle:** plan / design / split = **Opus**; execute / author = **Sonnet**; mechanical / fail-safe = **Haiku**; the judgment that *gates correctness* = **Opus**. **Fixes:** (1) `reviewer_critic` **T2→T1 Opus** — it's the correctness backstop and now has the diff (C1); bound the recurring cost by using T2 for intermediate fix-loop re-reviews and T1 for the decisive/final one. (2) `supply_chain_reviewer` **T1→T2 Sonnet** — mechanical SBOM/license check. (3) `principal_architect` + `senior_engineer`/tech-lead **stay T1 Opus** — they are the plan/design/split brains (this supersedes any earlier "tech-lead → T2" note). (4) `scope_judge` **stays T3**; bump to T2 only if runs under-size. (5) `appsec_engineer` **T1→T2 Sonnet** — its threat model is **build-dead** (the security gate re-scans the tree; nothing reads the model), so Opus gates nothing; Sonnet keeps the analysis readable for the UI/checkpoint. This **revises the earlier "appsec stays Opus"** — the verification showed its output feeds no gate. (`audit`/`supply_chain` already Sonnet; drop to Haiku if their narrative reads acceptably.) |
| A5b | **HOLD for a cost-min user (quality option, adds cost)** | The plan/do split saves money only when the baseline is Opus-everything with a big "do" phase (the original coder case). PM and qa are ALREADY single Sonnet calls (wave 1), and the coder runs on `claude-code`'s own CLI model — so adding an Opus *plan* call to each **increases** cost for a quality gain, contrary to the token-reduction goal. Implement ONLY if quality is prioritized over cost. Original intent (unchanged for reference): the Opus-plan → Sonnet-do split applies to every agent with BOTH a real planning decision AND a separate high-volume authoring phase: **IC coder** (A1), **`product_manager`** (Opus derives + completeness-checks the acceptance criteria — preserving the Opus judgment folded in from the criteria critic, D4 — then Sonnet writes the PRD prose), **`qa_engineer`** (Opus plans oracles/strategy → Sonnet writes tests). **NOT `sdet`** — its E2EReport is build-dead (the E2E gate re-derives), so an Opus plan step would be wasted; it's Haiku (D5). This supersedes A5's flat "qa → Sonnet". Everyone else stays single-tier: all-plan roles (architect, tech-lead) are already Opus with no bulk do-phase; all-mechanical / build-dead / single-verdict roles have no two-phase shape worth Opus. |
| B1 | **Adopt — keep proxy, nix adapter** | Keep the `ModelProviderProxy` as-is for all reasoning/judge agents; only swap the coding substrate from `proxy-agent` to **`claude-code`**. Deletes the quadratic; ⇒ A2/C3 moot, B3 gone. Trade: the coding call doesn't ride the proxy (auth in cage; cost read from the CLI envelope). |
| B2 | **Defer — design-first, CODING AGENT ONLY** | **Stacks on B1 (requires it): you can only resume a session if coding already runs on `claude-code`. Ship B1 first; B2 is a later add-on, not an alternative.** Within B2 pick ONE mechanism (`--resume`), not both. Cross-fix session continuity: reuse the coding session on a fix attempt instead of re-priming. **Scope: the coding agent (`junior_engineer`) only.** NOT judges/reviewers (session memory breaks verdict independence + temp-0 reproducibility — same reason per-agent threads are rejected, §5); N/A for single-shot agents (nothing to resume) and the orchestrator (it's the deterministic LangGraph supervisor, not a model caller — no session exists). Within a single task, `claude -p` is *already* a continuous session; B2 only adds continuity *across* fix attempts. Two mechanisms: **`--resume <session_id>`** (re-attach a fresh invocation — tolerates the graph's checkpoint pauses + hours-long rate-limit waits) vs **bidirectional stream-json** (one live process fed over stdin — can't survive those pauses/resume). **Prefer `--resume`.** Hard problem either way: the sandbox is destroyed + reset-to-base per task, so the worktree + session id must be kept alive across the fix loop and stored durably for graph resume. Principle: cross-agent/cross-iteration context rides the shared pool (explicit artifacts, auditable, deterministic); session memory holds only the coder's private within-task working history. Design doc + second opinion before building. |
| B3 | **Dropped** | N/A — the proxy is retained, so the verdict-signer/meter stays and the review/judge gates remain un-forgeable. No advisory-gate tradeoff to decide. |
| C1 | **Adopt** | Feed the reviewer the changed hunks (capped) via the `_retrieved_spans` resolve pattern. `security-review` the change. |
| C2 | **Adopt (incremental)** | Generalize `_ai_judge_evidence` into a per-role context contract; roll out node-by-node, non-blocking. |
| C3 | **Moot under B1** | `proxy-agent` is removed, so there is no observation transcript to prune. |
| D1 | **Adopt — design-first** | Parallelize the 3 lanes. Prerequisites: thread-safe `EpochCounter`, order-independent reducers, pacing on. Plan Mode + second opinion before coding. |
| D2 | **Cut, scoped + router change** | Remove the assurance-stage `reviewer_critic`; if a security-focused review is wanted, replace with a pass scoped to security-agent findings, gated on "tree changed since the quality review." **Verified:** the assurance *gate* does not require the review verdict, but `_route_assurance` (the ROUTER) fail-closes on a missing fresh review verdict (`assurance.py:139-158,194`) — so the cut MUST also change `_route_assurance` to stop demanding an assurance-stage review, else the boundary halts. |
| D3 | **Downgrade, don't delete** | Keep Stakeholder Proxy + Eng PM as desks/narrative but move to **T3 or advisory/no-LLM** — preserves the office (E1), drops the token cost. Full delete only if UI removal is also wanted. |
| D4 | **Revised → downgrade, not fold** | Verification showed the critic is NOT redundant: its critique drives the revision loop that improves the **acceptance criteria the Spec gate reads** (build-load-bearing), and folding it would delete property tests (bounded revision, advisory-not-verdict) and a security-completeness check. So instead: downgrade its tier **T1→T2** (env `FOUNDRY_CRITERIA_TIER=T1` restores Opus) — keeps the check + loop, cuts the cost. Implemented. |
| D5 | **Reframed → downgrade sdet** | Verified: `sdet` does NOT execute E2E (it authors a report via one model call); the assurance E2E gate re-runs pytest itself and reads nothing from sdet. So there is **no duplicate execution** to remove. The `E2EReport` is build-dead advisory (docs-only), so the real action is downgrade `sdet` **T2→T3**. Implemented. |
| D6 | **Adopt** | Downgrade `docs` to **T3** and make it async / non-blocking on the release path. |
| D7 | **Adopt — design-first** | Fan out the quality + assurance author chains. Ships with D1's `EpochCounter` fix + pacing. |
| E1 | **Adopt** | Dynamic per-room staffing off the live node set (roster follows D's cuts automatically). |
| E2 | **Adopt** | Give `scope_judge`, `ux_design`, `domain_specialist` desks (seat only when they run). |
| E3 | **Adopt** | One unique desk+sprite **per live IC** + per-lane Tech-Lead; floor scales with the run. |
| E4 | **Seat the Writer** | Promote `docs` to a seated agent so the Technical Writer desk is live (rather than dropping it). |
| E5 | **Remove all cosmetic** | Drop gate guard, pool runners, **and the CTO**. Replace the CTO's checkpoint role with a **non-character checkpoint modal/prompt** (design before E5 lands). |
| E6 | **Adopt** | Unique appearance per agent via procedural tinting/variation (sprite sheet lacks enough distinct frames for a dynamic IC count). |
| E7 | **Adopt (UI runs)** | Run the `claude-code` coding adapter with `--output-format stream-json --verbose` + `on_activity` wired, so the live per-agent activity phrases feed the floor. Observability only — does **not** change tokens or the quadratic. Headless/CI runs keep plain `--output-format json`. Distinct from bidirectional *input* stream-json (that's B2, session continuity). |
| E8 | **Adopt (UI) — kickoff "compose" redesign** | The `START A BUILD` modal (`index.html:79`, `.dialog.build`) crams request + full spec + all config into one 600px-wide vertically-scrolling column, and the most-used field (`#req-input`, `rows=3`) gets the least room. Invert it: treat the free-text as the **canvas**, config as a **rail**. Widen `.dialog.build` to `min(~1040px,96vw)`; two-pane at ≥900px (editor left, config right); spec/request editor tall by default (`min-height ~45–55vh`), monospace, **auto-grow** on input, expand toggle; collapse mode/build-as/adapter/stack behind the existing `details.fset.fold` accordion (only the editor open by default); make `.dialog-actions` sticky like the head; single-column on portrait with editor `min-height:50vh`. Stretch: promote spec authoring out of a modal into a full-page **compose** view. UI-only, low blast. |

## 4. Sequencing

1. **Now (low blast, high return):** A1, A2, A3, C1. Independent, individually
   shippable, each with a narrow test. A1+A2 are most of the token win; C1 is
   most of the *wasted-loop* win.
2. **Next:** A4, B1, D2, D3, D4, D6 — each a self-contained change behind its own
   verification. B1 unlocks retiring the A2 hog entirely.
3. **Design-first (high blast, needs Plan Mode + second opinion):** D1 (lane
   parallelism) and B2 (cross-fix session resume). Write a short design doc for
   each — state-reducer/determinism/resume implications for D1; worktree +
   session lifecycle for B2 — before touching code. **B2 stacks on B1 and must
   follow it** (no session to resume until coding runs on `claude-code`); B2 is
   an incremental win on the coder's fix loop, not a prerequisite for anything.
4. **UI (Workstream E), independent of the above:** low blast, UI-only. E1-E4/E6
   can follow whenever; E5 (remove cosmetic incl. the CTO) is blocked on
   designing the replacement checkpoint-approval affordance. Land E *after* the
   Workstream D cut decisions so the live-node-set roster reflects the final
   agent set in one pass.

## 5. Explicitly rejected: persistent session per reasoning agent

Do **not** convert the reasoning agents (PM, architect, reviewer, judges) to
long-lived per-agent sessions. They are stateless single-shot by design; a
persistent session would **accumulate** context they deliberately avoid
(increasing tokens and corrupting judgment independence), break per-call
lineage/drift observation and temperature-0 determinism, and fall outside the
LangGraph checkpoint so a resume couldn't restore it. Their token cost is already
addressed by SOP caching + A3, not by session reuse. The long-lived-CLI idea
belongs to the coding substrate (B1/B2) only.

## 6. Open questions / verify-before-cut

- **Dead-output confirmation (D3) — RESOLVED.** Trace confirmed `intent_brief` and
  `sprint_plan` are read only by checkpoint display + webapp UI (not the PM, not the
  ledger, not docs). Downgrade rather than remove, to keep the office/checkpoint
  narrative. Also newly confirmed build-dead: the three security agents' analysis,
  `oracle_binding`, and (read by nothing at all) `domain_addendum`/`ux_spec` (D8).
- **Assurance reviewer inputs (D2) — RESOLVED.** `run_assurance_gates` does NOT
  require a review verdict, but `_route_assurance` does (fail-closed) — so the D2 cut
  must also patch the router. Captured in D2.
- **Lane parallelism invariants (blocks D1):** the serial-overlay "shared
  evolving tree" assumes ordered predecessor slices; confirm disjoint ownership
  makes cross-lane visibility unnecessary, and that keyed-union reducers +
  `rt.seq` stay deterministic under concurrent lane writes and a mid-run resume.
- **Trust sign-off for B1/C1/D2:** these touch the coding-credential locus and a
  gate input — run `security-review` on each.

## 7. Expected outcome

- Token: A1+A2 remove the quadratic Opus/thinking coding path (the dominant
  sink); A3 trims output ceilings everywhere; C1+D2+D4 remove the bogus and
  duplicated review spend that drives fix loops.
- Latency: D1 collapses the serial lane chain (the biggest wall-clock item); D3/D4
  shorten the serial product chain; A4 bounds worst-case rework.
- No change weakens a gate: verdicts still come only from the attested
  `TwoZoneGateRunner`; the cuts target advisory/redundant model calls, not the
  trust boundaries.

## 8. Appendix — Agent → model map (current vs planned)

Tier legend: **T1 = `claude-opus-4-8`** (Opus), **T2 = `claude-sonnet-4-6`**
(Sonnet), **T3 = `claude-haiku-4-5`** (Haiku). Tiers are a cost choice, not a
trust choice; gates decide correctness regardless of tier. Several are
env-overridable (`FOUNDRY_REVIEW_TIER`, appsec/supply-chain envs).

| Agent | Layer | Current | Planned | Change driver |
|-------|-------|---------|---------|---------------|
| scope_judge | Product | T3 Haiku | T3 Haiku | — |
| stakeholder_proxy | Product | T2 Sonnet | **T3 Haiku / advisory** | D3 (dead output — downgrade, keep desk) |
| product_manager | Product | T2 Sonnet | **Opus (plan/criteria) → Sonnet (prose)** | A5b + D4 (keeps the critic's Opus judgment) |
| criteria_completeness_critic | Product | T1 Opus | **T2 Sonnet** (env T1) | D4 (downgraded, kept — drives the revision loop) |
| eng_program_mgr | Product | T2 Sonnet | **T3 Haiku / advisory** | D3 (dead output — downgrade, keep desk) |
| principal_architect | Architecture | T1 Opus | T1 Opus | — |
| domain_specialist *(conditional)* | Architecture | T2 Sonnet | T2 Sonnet | — |
| ux_design *(conditional)* | Architecture | T2 Sonnet | T2 Sonnet | — |
| senior_engineer / tech-lead *(per lane)* | Implementation | T1 Opus | **T1 Opus** (plan/split brain) | A5 + parallelized (D1) |
| junior_engineer / IC coder *(per lane)* | Implementation | **proxy-agent, T1 Opus + adaptive thinking** | **plan: short Opus micro-step → do: Sonnet `claude-code` (Opus on last fix), no thinking** | A1 + A5 + B1 (kills the quadratic) |
| reviewer_critic *(quality boundary)* | Quality | T2 Sonnet | **T1 Opus + sees the diff** (T2 for intermediate re-reviews) | C1 + A5 |
| qa_engineer | Quality | T1 Opus | **Opus (plan oracles) → Sonnet (write tests)** | A5b |
| sdet | Quality | T2 Sonnet | **T3 Haiku** (E2EReport build-dead; gate re-runs E2E) | D5 |
| appsec_engineer | Assurance | T1 Opus | **T2 Sonnet** (threat model is build-dead) | A5 |
| audit_compliance | Assurance | T2 Sonnet | T2 Sonnet | — |
| supply_chain_reviewer | Assurance | T1 Opus | **T2 Sonnet** | A5 |
| reviewer_critic *(assurance boundary)* | Assurance | T2 Sonnet | **cut / scoped to security findings** | D2 (duplicate review) |
| technical writer (docs) | Docs | T2 Sonnet | **T3 Haiku, async** | D6 |
| AI release judge *(per domain)* | Release | T2 Sonnet | T2 Sonnet | — |

Non-model (no tier): `triage`, the supervisor/orchestrator routing, every gate
runner (spec/build/test/assurance), the design/release sign-offs, and the
`*_fix` nodes — all deterministic code.

Pre-pipeline model users (outside the main build graph): the ideation/brainstorm
loop and the interview affordance both run **T2 Sonnet**.
