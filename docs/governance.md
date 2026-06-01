# Governance — gates, guardrails, security & compliance

Foundry's reliability comes from **a deterministic decision rule at the seams**
and from defending not just the control plane we built (gates, reducers,
monitors, ledger) but the **artifacts the agents author that become trusted
inputs to non-agent systems** — build/CI/IaC config, scanner config, and
acceptance criteria — plus the **verification/admission steps** that bind a
signature to "actually passed the gates." This document defines the gates, the
five-plane guardrail model, the monitor design, the supply-chain trust root and
deploy-time admission, the budget/convergence controls, and the domain-pack
mechanism.

## 1. Quality gates

A gate returns `pass | fail | error` and writes a `GateResult` (with a monotonic
`seq`; latest-by-`seq` wins — see [architecture.md §3](./architecture.md)).
`error` is an infrastructure failure, retried per-branch, not a verdict.
Per the re-verification rule, any code change re-keys integrated gates on a new
`tree_sha` and re-runs **all** integrated gates.

| Gate | Owner | Runs against | Decision rule | Pass condition |
|---|---|---|---|---|
| **Spec** | Product Manager (+ independent criteria critique) | PRD | structural + LLM | Every story has testable Given/When/Then criteria; **security/abuse criteria present where applicable** (omission fails the gate); bugfix = repro + expected behavior |
| **Design** | Architect (+HITL) | design artifacts | LLM + human | ADRs, interfaces, data model, `ownership_map`, `pinned_constraints` defined; human sign-off for significant designs |
| **Build (per-branch + integrated)** | CI runner | task branch, then `tree_sha` | deterministic | Compiles, lints, type-checks: zero errors |
| **Test (per-branch + integrated)** | QA (oracle authored separately from implementer; cross-family on high-risk) | task branch, then `tree_sha` | deterministic over possibly-flaky execution | All green; **every acceptance criterion exercised by a test that *fails against a known-bad reference* (criterion fidelity, not just coverage)**; line coverage ≥ threshold; quarantine rule (§1a) |
| **Security** | AppSec | `tree_sha` | deterministic scans + LLM triage | No unresolved high/critical (SAST, secret, SCA), **scans include build scripts + IaC**; threat-model items resolved |
| **Audit** | Compliance | `tree_sha` | deterministic | SBOM: no disallowed-license/critical-CVE deps; license compatibility; mapped to controls |
| **Supply-chain** | AppSec + DevOps | `tree_sha` | deterministic policy | Build/CI/IaC/Dockerfile/lockfile/scanner-config changes conform to policy (§4); no new build-time network/hooks, no privilege/egress widening, no signing/OIDC-trust changes without HITL |
| **E2E** | SDET | `tree_sha` | deterministic over possibly-flaky execution | End-to-end scenarios green (flake policy §1a) |
| **Release** | Release Manager (HITL) | already-attested artifact | human + evidence pack (§6) | All gates green; SLSA provenance + signature present with **gate-state bound in the predicate**; changelog + version + rollback plan present |
| **(Admission — not a graph gate)** | Non-agent admission controller | deployed digest | deterministic policy | At deploy: signature verifies **and** all required gates PASS for *this exact digest/commit/builder* (§5) |

Nine in-graph gates (Spec, Design, Build, Test, Security, Audit, Supply-chain,
E2E, Release) plus a deploy-time **admission** control. The "Owner" of a
deterministic gate is the agent that *triages* findings; the authoritative
**verdict** is computed and signed by the trusted runner, not the agent (§1c).

### 1a. Execution is not always deterministic — and that's handled

- **Flake policy:** tests run with pinned seeds, frozen clock, mocked network.
  Fail-then-pass within a retry budget of *N=2* → **flaky → quarantined**. A
  quarantined test covering a high-risk `criterion_id` **blocks**; any other
  quarantine needs **recorded QA acceptance + rationale**.
- **Anti-"retry-until-green":** the Guardian keys on oscillating verdicts for the
  same semantic objective (§3), not on byte-identical diffs.
- **Mutation testing** runs at the integrated gate / on a schedule (off the hot
  path); it validates test *sensitivity*. **Criterion fidelity** (each high-risk
  criterion's test must fail against a known-bad reference implementation) is the
  per-criterion check that coverage alone can't give.

### 1b. Test- and criteria-integrity (breaking the LLM-judges-itself loop)

- **Separation of authorship:** the model that authors acceptance tests differs
  from the implementer; for high-risk criteria the reviewer/QA run a **different
  model family** (not just tier) to decorrelate blind spots
  ([evaluation.md §4](./evaluation.md)). *(AI3)*
- **Criteria completeness is independently critiqued** before the Spec gate (a
  different role/family critiques the PM's criteria for completeness, especially
  security/abuse cases). Weak/omitted criteria fail the Spec gate — so "100%
  criterion-coverage" cannot be satisfied by under-specifying. *(SEC8)*
- **Fidelity over coverage:** criterion→test linkage is sample-audited for genuine
  semantic coverage; tags are not trusted blindly. *(AI6, SEC8)*
- **Review of test *and oracle-config* diffs** (see §3 deterministic monitors).

### 1c. Deterministic gate *verdicts* are computed by trusted infra, not claimed by agents

The attest→admit chain (§4–§5) treats "all gates PASS" as ground truth, so the
verdict bit cannot be an agent claim. For every **deterministic** gate — Build,
**Test, Security, Audit, Supply-chain, E2E** — the authoritative
`GateResult.status` is computed by a **trusted gate runner**, never by the agent.

**The runner is two-zone** (mirroring the builder's input/environment split),
because it must *execute agent-authored test/build/scan code* yet *sign a verdict*
— and those two must not happen in one process: *(SEC-R9-F1)*
- **Zone A — untrusted executor:** runs the agent-authored tests, build steps, and
  any agent-supplied scanner rules/plugins in a sandbox with **no access to any
  signing credential or OIDC token**, emitting only a one-way **raw results
  stream** (exit codes, per-test outcomes, scanner SARIF).
- **Zone B — trusted attestor:** holds the signing identity, **executes no agent
  code**, and computes/signs the verdict as a deterministic function of zone-A's
  results stream evaluated against the **pinned external config**. The signing
  identity is **unreachable from zone A** (same property the builder already
  asserts for sandboxes). RCE-from-a-malicious-test-to-runner-signature is in the
  red-team set.

The signature is **bound to its inputs** — `runner_attestation` signs
`(gate_name, tree_sha, scanner_config_revision_hash, status, results_digest)` —
so a valid attestation cannot be replayed onto a different tree/config/predicate;
admission re-checks **all** of these per record, not just the runner identity.
*(SEC-R9-F3)*

Agents only *triage and explain* (writing `GateTriage`, never `GateResult`, for
deterministic gates — [architecture.md §3](./architecture.md)); the `gates`
reducer **rejects any unsigned deterministic-gate write**, so an agent cannot
supersede the runner's verdict even in the live channel that in-graph routing and
the evidence pack read. The WORM appender and builder accept a deterministic
verdict only with a valid attestation. Only the LLM-judgment gates (Spec, Design)
are agent/human verdicts, defended by the independent critic + HITL. *(SEC-R8-F1, DS-R9-F2)*

## 2. The five-plane guardrail model

### Policy plane
- OPA authorizes every tool call (least privilege per role; only the trusted
  builder reaches signing; only the non-agent deployer deploys).
- **Policy applies to artifact *outputs*, not just tool calls:** IaC `plan`
  diffs are gated by Conftest/Checkov (no privilege grants, new IAM principals,
  egress widening, or OIDC-trust/signing-identity changes beyond a declared
  baseline; such changes require HITL). Writing a Terraform file is an allowed FS
  call, but applying its effect is not. *(SEC5)*
- Scanner configuration and suppression files (`.semgrepignore`, `# nosec`,
  `.trivyignore`, `.gitleaks.toml`, severity floors, license allowlists) live in
  a **protected, repo-external, run-immutable** path agents cannot write; any new
  suppression/floor/allowlist edit is a **first-class finding** routed to HITL and
  counted by a deterministic monitor. Scanners run from the pinned external
  config, so agent diffs cannot weaken them. *(SEC2)* **The external
  scanner-config repository is under a separate trust boundary with no agent write
  or PR path** (human-only / distinct credentialed owner); changes require
  two-person sign-off, and its **current pinned revision hash is recorded in the
  evidence pack and the SLSA predicate**, so a config swap is detectable. *(SEC-R8-F3)*

### Execution plane
- All agent-written code runs in sandboxes (containers; Firecracker/gVisor for
  stronger isolation); never on the host.
- **Credentialed phase is separated from untrusted-code execution:** no
  agent-authored code or third-party install/`postinstall` script runs while task
  credentials are present. Dependencies are pulled through an **internal mirror
  of pre-vetted, hash-pinned artifacts**, so builds need no open egress. *(SEC4)*
- **Mirror admission is a non-agent / HITL-gated control** (hash-pinning gives
  reproducibility, not trustworthiness, of a *newly* admitted package). Any new
  package or version entering the mirror passes a vetting step **distinct from the
  in-repo lockfile policy check**: provenance/signature verification,
  **namespace reservation against dependency-confusion / typosquat**, and — for
  any package lacking an existing trusted attestation (e.g. an upstream SLSA
  predicate / reproducible-build proof) — a **mandatory** source-build-and-diff
  against upstream (*not* optional, since a human cannot adjudicate "is this
  source benign" from nothing). A **new-dependency request is a first-class,
  HITL-acknowledged change class** whose evidence pack surfaces upstream
  provenance, maintainer/namespace history, diff-from-prior-version, and the
  transitive-closure delta — paralleling the release evidence pack — not a
  lockfile line the Supply-chain gate waves through. *(SEC-R8-F2, SEC-R9-F2)*
- Egress is default-deny **through per-destination proxies with payload/size
  anomaly detection** — host allowlisting alone does not stop exfil over
  allowed channels (registry, git remote, model provider). The sandbox is treated
  as hostile. *(SEC4)*

### Data plane
- Scoped, short-lived credentials per task; **never in prompts or state**;
  signing keys unreachable from any agent sandbox.
- **Data boundary to providers/trace tooling is explicit:** zero-retention /
  no-train provider terms; a self-hosted or scrubbed trace backend for regulated
  runs; **redaction runs *before* provider egress** (not only on stored traces),
  with a tested, **fail-closed** redaction component. Per-domain-pack rules apply
  (PHI/PCI data never leaves the boundary). *(SEC10)*

### Observability plane
- OpenTelemetry traces every superstep (node, tool, args, tokens, USD, latency,
  gate results, policy denials, serving-model fingerprint). Feeds the monitors
  and the eval harness; runs are replayable for *control state* (not bitwise LLM
  output — [architecture.md §8](./architecture.md)).

### Assurance plane
- SBOM, SLSA L3 provenance, signing — produced by the trusted hermetic builder
  with **gate-state bound into the predicate** (§5).
- Red-teaming (prompt injection, tool abuse, sandbox escape, **build-hook
  injection**, **exfil-over-allowlist**).
- Evals run in CI on every change to prompts, graph, tools, gates, or scanner
  config ([evaluation.md](./evaluation.md)).

## 3. Monitor design (deterministic monitors + event-triggered analyzer)

- **Deterministic monitors (no LLM, not prompt-injectable) carry the load-bearing
  detections:**
  - *Loop / non-convergence* — semantic signature `(criterion_id/task, gate,
    failure_signature)`; plus the global `fix_attempts_since_all_green` bound
    ([architecture.md §7](./architecture.md)). Diff bytes are never the signature.
  - *Oracle / gate / criteria / scanner-config edits* — **any diff touching tests
    treated as oracles, gate logic, acceptance criteria, or scanner config is a
    *deterministic* trip** forcing HITL review (not an analyzer judgment). *(SEC3, AI10, SEC2)*
  - *Cost* — `BudgetLedger` thresholds; *Policy* — OPA deny-count;
    *Retry-until-green* — verdict oscillation.
  - *Pinned-constraint pressure* — the relevant `pinned_constraints` set for a
    node approaching its injection budget escalates to HITL (never silent
    truncation, [architecture.md §8](./architecture.md)). *(AI-R8-F2)*
  - *Criterion-coverage delta* — computed against signals the implementing agent
    does **not** solely control: the baseline is the **Spec-gate-frozen
    (immutable) criterion set**, and the satisfied side is the **count of
    high-risk `criterion_id`s whose runner-attested negative-oracle/mutant family
    passes** (computed by the trusted runner, §1c) — **not** the agent-authored
    test-tag count (which a coordinated scope-reduction could co-shrink). A drop
    in that runner-attested count against the frozen baseline trips deterministically.
    *(AI-R8-F3, AI-R9-F2)*
- **Sandboxed LLM analyzer (semantic color only):** runs read-only in its **own
  toolless sandbox**, fired by (a) a deterministic-monitor signal, (b) a write to
  oracle/gate/criteria/config paths, **and (c) a low-rate *sampled* pass over a
  random subset of ordinary supersteps** — so semantic reward-hacking that stays
  under every deterministic bound and touches no protected path is still
  observed. It reads a **capped** slice of **deterministically-extracted
  structured features**, with untrusted trace prose delimited as *data, never
  instructions*; it emits **advisory** flags only and cannot approve work or
  override a deterministic trip. Precision/recall — **including against
  prompt-injection-of-the-monitor and on an *untriggered* held-out set** — is
  evaluated ([evaluation.md §4](./evaluation.md)); its sampled+triggered
  invocation count and token cost are sized into the per-feature cost model
  ([evaluation.md §8](./evaluation.md)) and metered as `monitor_overhead_usd`. *(SEC3, AI4, AI10, AI-R8-F3)*

## 4. Supply-chain trust root (build, sign, attest)

The builder is trusted but **what it executes is agent-authored**, so the trust
boundary is enforced on the build *inputs*, not just the build *environment*:

- **Strong hermeticity:** the release build runs on a pinned base image with
  **no network egress**, vendored/hash-pinned dependencies, and **no arbitrary
  build hooks** (`postinstall` network, `RUN curl | sh`, unreviewed custom CI
  Actions are disallowed by the Supply-chain gate). *(SEC1)*
- **Build config is a high-privilege artifact:** changes to CI/IaC/Dockerfile/
  lockfile/manifest/scanner-config are diffed against a baseline and routed
  through the **Supply-chain gate** (and HITL for trust-root-touching changes),
  separately from the application Security gate. The Security gate's SAST also
  scans build scripts and IaC. *(SEC1)*
- **Keyless signing (OIDC/sigstore):** no long-lived keys to exfiltrate; identity
  is the CI workload's OIDC token. OIDC trust/federation policy is itself IaC and
  is in the policy-plane's HITL-required set (§2). *(SEC5)*
- **Provenance predicate binds gate-state:** the signed in-toto/SLSA predicate
  records source commit, builder identity, hermetic inputs, materials, the
  **pinned external scanner-config revision hash**, **and the WORM-anchored,
  hash-chained gate results keyed to this `tree_sha` — each carrying the trusted
  runner's attestation (§1c)** — so the signature attests *both* provenance *and*
  "passed all required Foundry gates, as computed by trusted infra." Target
  **SLSA Build L3**. *(SEC7, SEC-R8-F1, SEC-R8-F3)*
- **SBOM pass criteria:** no critical/high-CVE dependency (configurable floor),
  no disallowed license, full resolution (no unpinned components).

## 5. Deploy-time admission (signature ≠ safety, so verify on admission)

A signature proves provenance, not that deployment is authorized. Therefore:

- After the human Release sign-off, a **non-agent admission controller**
  cosign-verifies the signature **and** evaluates a policy: *this exact digest was
  built by the trusted builder, from this commit; all required gates PASS for it
  in the WORM store **with valid input-bound trusted-runner attestations (§1c)**;
  and the scanner-config revision matches the pinned hash*. Only then does a
  **separate, least-privileged, non-agent deployer** ship.
- **Fail-closed across runs/forks:** admission treats **any FAIL for a `tree_sha`
  as disqualifying, regardless of a later PASS** — so two forks that produce the
  same digest with opposite verdicts (e.g., a newly-admitted CVE flips Security)
  can never ship the green one. This removes any dependence on cross-run verdict
  ordering. An agent that gets an unverified, forged-verdict, or non-gate-passing
  tree built cannot reach production. *(SEC7, SEC5, SEC-R8-F1, DS-R9-F1)*

## 6. Human-in-the-loop is evidence-backed, not a rubber stamp

HITL is load-bearing (Design, Release, escalations), so the human is given an
**independent, non-agent-generated evidence pack**: a deterministic diff of
**security-relevant paths** (build/CI/IaC, scanner config, deps/lockfile,
signing/OIDC policy), the **raw** Guardian flags (not agent-summarized), the list
of any **new suppressions/severity-floor changes**, the actual provenance
predicate and gate-state, and the convergence/cost status. High-risk change
classes require explicit per-class acknowledgment; releases touching the
trust-root surface require **two-person sign-off** with break-glass logging.
Minimum review affordances guard against automation bias. *(SEC6)*

## 7. Budget & convergence controls (concrete)

- **Enforced at superstep boundaries** (LangGraph cannot checkpoint mid-node);
  long node bodies poll a cooperative-cancellation token and persist partial
  progress before yielding ([architecture.md §6](./architecture.md)). *(DS6)*
- **Per-phase sub-budgets** (defaults, tuned in Phase 5): Product 5%,
  Architecture 15%, Implementation 35%, Verification 15%, Assurance 10%, Release
  5%, **Rework reserve 10%**, monitor overhead ≤ 5% — 100% *with* contingency.
  **Triage seeds a default per-route cap**; PLAN refines it on greenfield/evolution.
- **Per-loop caps draw from the Rework reserve**, not the owning phase, so
  designed-in rework doesn't auto-breach a phase cap. A **global non-convergence
  bound** ([architecture.md §7](./architecture.md)) escalates a non-converging
  cross-gate cascade as *non-convergence*, not as a late budget breach. *(DS5)*
- **Monitor overhead is modeled, not asserted:** a per-feature cost model
  ([evaluation.md §4](./evaluation.md)) derives the analyzer's event-triggered
  invocation count and per-call input cap; if 5% proves infeasible the target is
  raised honestly rather than throttling detection blindly. *(AI4)*
- **No tier downgrade on T1 nodes** — breach escalates.
- **Caching:** stable SOP/system prefixes use prompt-cache breakpoints.

## 8. Audit-store integrity

- Audit/compliance events are written to an **append-only WORM / hash-chained
  store via a non-agent appender** holding a credential **no agent role
  possesses**; no agent can rotate, truncate, or re-initialize the chain. *(SEC9)*
- The chain **head is periodically anchored externally** (transparency
  log / external timestamp) so truncation or rewrite is detectable. *(SEC9)*
- Events reach the store via the transactional outbox ([architecture.md §5](./architecture.md)),
  so checkpoint and audit cannot diverge.

## 9. Domain packs

A Specialist is a conditional node implementing `{capabilities, tools,
prompts/SOP, gates, evals}`; a registry maps required capabilities to specialists.

| Pack | Adds specialist | Adds gates / controls |
|---|---|---|
| Fintech | Compliance specialist | PCI-DSS/SOX in Audit; stricter crypto review; data never leaves boundary |
| Healthcare | Privacy specialist | HIPAA mapping; PHI data-plane rules (no PHI to providers) |
| ML / Data | ML engineer | Data-validation + model-eval gate; dataset license checks |
| Mobile / Embedded | Platform specialist | Platform build/sign gates; resource budgets |

## 10. Failure & escalation policy

1. Gate `fail` → responsible role, increment retry counter.
2. Gate `error` (infra) → bounded per-branch retry → else escalate (no sibling loss).
3. Code change → new `tree_sha` → re-run *all* integrated gates.
4. Retry cap or **non-convergence bound** reached → Human Liaison (right diagnosis, not a budget breach).
5. Deterministic-monitor trip (loop / oracle-or-config edit / policy / cost) → pause + HITL; analyzer flags are advisory.
6. Budget breach → escalate (T1 never downgrades).
7. All escalations resume from the last checkpoint; effectful actions re-verify by idempotency key and never re-fire.
