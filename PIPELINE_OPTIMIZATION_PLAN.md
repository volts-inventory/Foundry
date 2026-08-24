# Pipeline Token & Latency Optimization Plan

Status: **decided, not yet implemented.** A selected approach is recorded for
every item in §3 "Decisions." The mediating proxy is being **removed entirely**
(B1); one open point (**B3**) needs confirmation — whether to keep a minimal
verdict-signer+meter so the review/judge gates stay un-forgeable, or drop it too
and let those gates become advisory. Each item lists its own verification and
blast radius; the high-blast-radius items (proxy removal, D1, D7, B2) are marked
for Plan Mode + a second-opinion pass before any code lands.

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

**Decision revision (supersedes B1's "coding only"):** remove the mediating
`ModelProviderProxy` egress **entirely** — every agent (reasoning *and* coding)
calls Claude Code directly with its own sandbox auth. The proxy adds no tokens
(it is a stateless pass-through), so this is a **token-neutral** simplification;
the payoff is operational — fewer moving parts, no single middleman, each agent
talks to Claude directly. It also deletes the `proxy-agent` adapter and its
quadratic transcript outright, so **A2 and C3 become moot** (there is no external
ReAct loop left to bound).

The catch — the proxy owns four responsibilities that do **not** vanish with it;
each must be re-homed or explicitly dropped:

1. **Verdict attestation** (the `ZoneBSigner` stamp on `proxy.judge`) — the thing
   that makes the reviewer / AI-judge `approve` **un-forgeable by the agent**
   (LEARNINGS L5). Drop it → the review/judge gates become **advisory**: an
   agent's self-reported verdict is trusted. This is a security-posture change.
2. **Lineage observation + pinned-ID drift** detection.
3. **Per-call usage metering** — feeds the budget ledger and the UI cost meter.
4. **Retry / rate-limit / backoff** (429 + Max-window waits).

Recommended realization of "get rid of the proxy": **collapse the egress, keep a
minimal signer+meter seam** (a ~small component, not a middleman) so the gates
stay trustworthy and the UI still shows cost; let the `claude` CLI own retry and
auth. Fully deleting the signer too is possible but makes the trust gates
advisory — that specific point is flagged for confirmation (decision **B3**).

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
| D5 | **De-dupe E2E.** `sdet` authors an E2EReport for the build/test boundary and the assurance E2E gate re-runs E2E at HIGH tier — run E2E once and share the attested result. | `sdet.py`; `assurance_wiring.py:138-139` | Low–Medium | Medium |
| D6 | Make `docs` (T2, every PASS) cheaper (T3) or async — it's on the release critical path but not release-gating. | `factory_graph.py:1158-1166` | Low | Low |
| D7 | **Fan out the independent author chains.** Quality (`reviewer_critic`/`qa_engineer`/`sdet`) and assurance (`appsec`/`audit`/`supply_chain`) run as serial chains but the nodes are independent — they read the same integrated diff and don't consume each other's output. Run each subgraph's authors concurrently in one superstep (as architecture already does for domain_specialist + ux_design), converging at the gate. | `quality.py:112-115`; `assurance.py:486-490` | Medium (latency) | Medium — same reducer/`Seq` concurrency concerns as D1 (see track below), but no cross-file mutation. |

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
| A1 | **Adopt** | Default the coding adapter to **T2 (Sonnet)**; escalate to **T1 (Opus)** only on the final fix attempt. Drop `purpose="ideation"` on the coding path (no adaptive thinking). |
| A2 | **Moot under B1** | B1 deletes `proxy-agent` entirely, so there is no external transcript to bound. `claude-code` manages its own context. |
| A3 | **Adopt** | Per-purpose `max_tokens`: gate/judgment/structured-verdict **2k**, decomposition/structured **4k**, free-form ideation **8k** (down from a flat 16k). |
| A4 | **Adopt** | CLI `max_fix_attempts` default **5→3** (match the graph); wire `cost_cap_microusd` as an early stop. |
| B1 | **Adopt — proxy removed** | **Remove the mediating proxy egress entirely** — all agents (reasoning + coding) call Claude Code directly. Token-neutral simplification; deletes the `proxy-agent` adapter (⇒ A2/C3 moot). |
| B2 | **Defer** | Write a design doc first (worktree + `session_id` lifecycle vs reset-to-base idempotency). Not in the first implementation wave. |
| B3 | **NEEDS CONFIRM** | Verdict attestation + metering (the proxy's non-token jobs). **Recommended:** keep a minimal signer+meter seam so review/judge gates stay un-forgeable and cost is still metered; the CLI owns retry/auth. **Alternative (full nuke):** drop the signer too → review/judge gates become **advisory** (agent verdicts trusted directly). Confirm which. |
| C1 | **Adopt** | Feed the reviewer the changed hunks (capped) via the `_retrieved_spans` resolve pattern. `security-review` the change. |
| C2 | **Adopt (incremental)** | Generalize `_ai_judge_evidence` into a per-role context contract; roll out node-by-node, non-blocking. |
| C3 | **Moot under B1** | `proxy-agent` is removed, so there is no observation transcript to prune. |
| D1 | **Adopt — design-first** | Parallelize the 3 lanes. Prerequisites: thread-safe `EpochCounter`, order-independent reducers, pacing on. Plan Mode + second opinion before coding. |
| D2 | **Cut, scoped** | Remove the assurance-stage `reviewer_critic`; if a security-focused review is wanted, replace with a pass scoped to security-agent findings, gated on "tree changed since the quality review." Verify `assurance_gate` doesn't require a fresh `review` verdict first. |
| D3 | **Downgrade, don't delete** | Keep Stakeholder Proxy + Eng PM as desks/narrative but move to **T3 or advisory/no-LLM** — preserves the office (E1), drops the token cost. Full delete only if UI removal is also wanted. |
| D4 | **Adopt** | Fold the completeness checklist into the PM SOP; drop the separate critic node + its revision loop. Keep the security/abuse coverage in the PM prompt. |
| D5 | **Adopt** | Run E2E once; share the attested result across the build/test and assurance boundaries. |
| D6 | **Adopt** | Downgrade `docs` to **T3** and make it async / non-blocking on the release path. |
| D7 | **Adopt — design-first** | Fan out the quality + assurance author chains. Ships with D1's `EpochCounter` fix + pacing. |
| E1 | **Adopt** | Dynamic per-room staffing off the live node set (roster follows D's cuts automatically). |
| E2 | **Adopt** | Give `scope_judge`, `ux_design`, `domain_specialist` desks (seat only when they run). |
| E3 | **Adopt** | One unique desk+sprite **per live IC** + per-lane Tech-Lead; floor scales with the run. |
| E4 | **Seat the Writer** | Promote `docs` to a seated agent so the Technical Writer desk is live (rather than dropping it). |
| E5 | **Remove all cosmetic** | Drop gate guard, pool runners, **and the CTO**. Replace the CTO's checkpoint role with a **non-character checkpoint modal/prompt** (design before E5 lands). |
| E6 | **Adopt** | Unique appearance per agent via procedural tinting/variation (sprite sheet lacks enough distinct frames for a dynamic IC count). |

## 4. Sequencing

1. **Now (low blast, high return):** A1, A2, A3, C1. Independent, individually
   shippable, each with a narrow test. A1+A2 are most of the token win; C1 is
   most of the *wasted-loop* win.
2. **Next:** A4, B1, D2, D3, D4, D6 — each a self-contained change behind its own
   verification. B1 unlocks retiring the A2 hog entirely.
3. **Design-first (high blast, needs Plan Mode + second opinion):** D1 (lane
   parallelism) and B2 (cross-fix session resume). Write a short design doc for
   each — state-reducer/determinism/resume implications for D1; worktree +
   session lifecycle for B2 — before touching code.
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

- **Dead-output confirmation (blocks D3):** grep the `webapp/` and any
  interview/event-log/office code for consumers of `IntentBrief` and
  `SprintPlan` before deleting the nodes. If only the narrative UI reads them,
  downgrade rather than remove.
- **Assurance reviewer inputs (blocks D2):** confirm `run_assurance_gates`
  doesn't require a `review` verdict minted at the assurance stage specifically.
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
