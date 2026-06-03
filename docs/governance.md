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
`tree_sha`; re-verification is **dependency-scoped (§1f)** — the gates the change
can affect re-run, while unaffected inputs reuse their cached runner-attested
verdict, so the attested gate-state still covers the whole tree.

| Gate | Owner | Runs against | Decision rule | Pass condition |
|---|---|---|---|---|
| **Spec** | Product Manager (+ independent criteria critique) | PRD | structural + LLM | Every story has testable Given/When/Then criteria; **security/abuse criteria present where applicable** (omission fails the gate); bugfix = repro + expected behavior |
| **Design** | Architect (+HITL) | design artifacts | LLM + human | ADRs, interfaces, data model, `ownership_map`, `pinned_constraints` defined; human sign-off for significant designs |
| **Build (per-branch + integrated)** | CI runner | task branch, then `tree_sha` | deterministic | Compiles, lints, type-checks: zero errors |
| **Test (per-branch + integrated)** | QA (oracle authored separately from implementer; cross-family on high-risk) | task branch, then `tree_sha` | deterministic over possibly-flaky execution | All green; **every acceptance criterion exercised by a test that *fails against a known-bad reference* (criterion fidelity, not just coverage)**; line coverage ≥ threshold; quarantine rule (§1a) |
| **Security (deterministic part)** | trusted runner (AppSec triages) | `tree_sha` | deterministic scans | No unresolved high/critical (SAST, secret, SCA), **scans include build scripts + IaC**; each threat-model item's **bound deterministic oracle passes** (§1d) |
| **Threat-review (judgment residual)** | AppSec + HITL | design + diff | LLM + human | Threat-model items that *cannot* be reduced to an oracle are resolved by independent review (like Spec/Design), **not** bound into the runner-signed verdict (§1d) |
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
- **Zone A — untrusted executor:** runs the agent-authored tests/build steps in a
  sandbox with **no access to any signing credential or OIDC token**.
- **Zone B — trusted attestor:** holds the signing identity, **executes no agent
  code**, and computes/signs the verdict. The signing identity is **unreachable
  from zone A**. RCE-from-a-malicious-test-to-runner-signature is in the red-team
  set.

But zone A holding no credentials is **not sufficient**: the verdict must not be a
deterministic function of an *agent-forged input*. A malicious test harness or
scanner plugin in zone A could self-report "all passed" / zero findings, which
zone B would faithfully sign — trust *relocated to the results stream*, not
grounded. So the verdict is grounded by gate class: *(SEC-R10-F1)*
- **Config-evaluable gates** (Build dependency/lockfile policy, Audit SBOM/CVE/
  license, Supply-chain, IaC-plan): **zone B re-derives the result itself** from
  the tree + pinned data — it never trusts a zone-A-emitted SBOM/SARIF/lockfile
  report.
- **Execution-dependent gates** (Test, E2E, and the SAST portion of Security):
  the **outcome observation is performed by trusted, pinned infrastructure**, not
  self-reported by agent code. Agents supply **test *sources*, never the scoring
  harness**; the test-runner/reporter binary and invocation, and the SAST scanner
  binary + rules (from the run-immutable external config — no agent-supplied
  plugins), are **pinned trusted components**. Zone B (or a trusted harness it
  controls) orchestrates execution and collects outcomes over a **sealed result
  channel in a separate FS namespace the agent code cannot intercept** (exit
  codes / JUnit XML emitted *inside* the agent process are treated as untrusted).
  Zone B **independently substitutes known-bad/mutant references and verifies the
  result digest differs** (pass-on-real, fail-on-known-bad). But the test is still
  agent-authored, so it could pass-on-real / fail-on-known-bad by **fingerprinting
  the mutant's *identity*** (a file/symbol hash, or the syntactic presence of the
  fix) rather than exercising *behavior* — testing nothing while satisfying the
  flip. Two reinforcing controls close this: *(SEC-R11-F1)*
  - **Held-out mutants:** zone B's grounding substitution is drawn from a
    **rotating held-out mutant family never exposed to the test-authoring model
    family** (secret until after attestation), so a fingerprint cannot be
    pre-baked against a known substitution. **The mutant generator must itself be a
    trusted, family-decorrelated component** — curated/non-LLM **plus** cross-family,
    **not the implementer/QA lineage** (a generator sharing that lineage shares its
    blind spots). The held-out family carries its **own adequacy/diversity floor**,
    and the **`mutant_rotation_id` is an opaque handle** that reveals nothing about
    set contents — even though it is bound into the public attestation/predicate +
    reuse key (below), it must not be a profileable index. **Rotation occurs faster
    than an adapter can profile the set across runs.**
  - **Behavioral-invariance check:** zone B runs the test against a **family** of
    independently-authored held-out behavioral mutants **and** against
    **behavior-preserving perturbations of the real tree** (renames, path/hash
    changes, equivalent refactors). The verdict is accepted only if PASS is
    **invariant under behavior-preserving change** and FAIL holds **across the
    mutant family** — a test that keys on identity rather than behavior flips on a
    behavior-preserving perturbation and is **rejected**.
  - **Substitutions are opaque at runtime:** behavioral-invariance still admits a
    runtime-active test that *chooses behavior after inspecting the tree* — a
    coding-agent adapter can emit test code that reads file hashes/AST/baseline
    diff at runtime to detect the substitution and branch its verdict, so it never
    flips on a perturbation it can see coming. So the substitution must be **opaque
    to the test at runtime**: identical test bytecode runs against real vs mutant in
    **indistinguishable environments** — a normalized tree with path/hash/identity
    oracles stripped, the test **denied reads outside the unit under test** and
    **denied any baseline to diff against**. **Runtime-environment-sensitive test
    sources are barred from the execution-deterministic class** (they fall to a
    judgment verdict), and a **trusted differential harness controls inputs and
    outputs** so the test cannot observe which side it is running against. *(SEC-R12-F1)*
  - **Value-pinned vacuous tests survive the flip — and a *lookup table* of them
    survives "more than one assertion."** The adapter authors **both** implementation
    and test, so it can hard-code the real output (`assert f(x) == <real output>`) —
    a single pinned assertion passes pass-on-real, fails across mutants, and is
    invariant under behavior-preserving perturbation while exercising essentially
    nothing. Flip-direction alone does not catch it, so the discriminator additionally
    requires **adequacy/diversity**: the test must **fail across a held-out mutant
    family broad enough that no single pinned assertion can cover it**, and the family
    uses **input-space mutants, not only code mutants**. But a "broad enough family"
    is **not** met by "more than one assertion": if the adapter can **enumerate or
    derive the input set** the held-out mutants predictably perturb (boundary/partition
    points), it can write a **finite TABLE of pinned assertions** `assert f(xᵢ) == rᵢ`
    over exactly those inputs — passes-on-real, fails-across-the-mutant-family,
    perturbation-invariant, yet still exercises nothing. So the floor is grounded on
    inputs the test **cannot** itself choose: zone B **holds out the
    input-sampling distribution** (secret and non-enumerable — not just the mutants,
    so the perturbed input set is not derivable), **and/or requires differential
    adequacy on TRUSTED-INFRA-FUZZED inputs the test did not select**. A **finite set
    of pinned assertions over a derivable input set does NOT clear class-2** — the bar
    is non-enumerable/fuzzed-input adequacy, not merely ">1 assertion." This
    **adequacy/diversity floor is bound into class-2 acceptance** — not only checked at
    the scheduled mutation gate (§1a) — and **single-point / value-pinned / lookup-table
    tests do NOT clear the execution-deterministic (class-2) class**. *(SEC-R12-F1)*

  This restores the needed property: the verdict is a function of *behavior
  observed by trusted infra*, not of agent-controllable environment identity.
  Until the *observation and the discriminator* are both grounded outside
  agent-controlled code, the attestation rests on a forgeable input — so these are
  hard requirements, not options.

The signature is **bound to its inputs** — `runner_attestation` signs
`(gate_name, tree_sha, scanner_config_revision_hash, mutant_rotation_id,
scanner_rule_version, lineage_map_revision, status, results_digest)` — so a valid
attestation cannot be replayed onto a different tree/config/predicate; admission
re-checks **all** of these per record, not just the runner identity.
(`lineage_map_revision` pins the criterion/threat-model→oracle binding the verdict
was grounded under; [architecture.md §3](./architecture.md) owns the
judgment-binding side — keep the term consistent.) Binding **mutant-rotation-id +
scanner-rule-version** into the tuple (and into the dependency-scoped reuse key,
§1f) means an execution-dependent verdict's anti-gaming grounding is pinned to the
exact rotation/rule-version it was grounded under. *(SEC-R9-F3, SEC-R12-F2)*

**Class-4 human-witnessed attestation is grounded with equal rigor.** *(SEC-R12-F5)*
The grounding machinery above (two-zone runner, sealed channel, `verify_attestation`)
covers only deterministic-gate verdicts; the **class-4 human-witnessed** verdict
([domains.md §1](./domains.md) — validation protocols, per-artifact
approver-of-record) carries an equal-rigor signature path so it cannot be minted or
replayed by an agent/orchestrator:
- The signature is produced **out-of-band against a federated IdP / hardware-backed
  PKI key the pipeline never holds** — the agent/orchestrator has no path to mint it.
- It **binds `(artifact_uri, state_sha, approver_identity, role, credential_ref,
  timestamp, server_nonce, presented_evidence_hash, meaning-of-signing)`**, so a
  signature cannot be replayed onto a different artifact/state or repurposed across
  meanings. The **`server_nonce` is a per-approval server-issued challenge**, verified
  for freshness at **reducer-time *and* admission** — timestamp alone permits replay
  within clock skew. The **`presented_evidence_hash` is the content hash of the exact
  non-agent-assembled evidence pack / predicate snapshot shown to the signer**, so the
  human signs over **what was presented** — closing the "what you see is not what you
  sign" gap that `artifact_uri`+`state_sha` alone leaves open.
- `verify_attestation` **validates class-4 signatures against the external IdP /
  cert chain at reducer-time *and* at admission** (not just the runner's own keys).
- A **class-4 write from any agent identity is rejected exactly like an unsigned
  deterministic-gate write** — the agent can never supply the human verdict bit.

Agents only *triage and explain* (writing `GateTriage`, never `GateResult`, for
deterministic gates — [architecture.md §3](./architecture.md)); the `gates`
reducer **rejects any unsigned deterministic-gate write**, so an agent cannot
supersede the runner's verdict even in the live channel that in-graph routing and
the evidence pack read. The WORM appender and builder accept a deterministic
verdict only with a valid attestation. Only the LLM-judgment gates (Spec, Design,
Threat-review) are agent/human verdicts, defended by the independent critic + HITL. *(SEC-R8-F1, DS-R9-F2)*

### 1d. "Threat-model items addressed" is not magically deterministic

"Addressed" cannot be a runner-computed bit by fiat. At the Design/Spec seam,
**each threat-model item is bound to a concrete, runner-evaluable artifact** — a
test, a SAST/Semgrep rule, or a criterion-fidelity negative oracle — so that
"addressed" = "its bound deterministic check passes," computed by zone B. The
AppSec agent's manual review produces only `GateTriage` (which items to add,
remediation notes), never the resolved/unresolved bit. Threat-model items that
**genuinely cannot** be reduced to a deterministic oracle are split out into the
**Threat-review judgment sub-gate** (defended by independent review + HITL, like
Spec/Design) and are **not** bound into the runner-signed Security verdict — so
the runner-signed gate stays honestly deterministic. *(DS-R10-F2)*

> **Verdict classes (core generalization).** The "deterministic oracle + judgment
> residual" split above is the *backend/logic* case of a general **verdict-class
> taxonomy** ([domains.md §1](./domains.md)): config-evaluable, execution-
> deterministic, **statistical/tolerance-band** (ML metrics, perceptual distance,
> performance budgets — a thresholded random variable the runner re-derives from a
> pinned eval set+seed), **human-witnessed/attested** (validation protocols,
> per-artifact approver-of-record — signed by a named credentialed human), and
> **perceptual/judgment** (look/feel, a11y-judgment, safety cases — mandatory HITL
> over a rendered/evidence bundle, *excluded from the signed predicate but still
> first-class and gated*). The held-out-mutant + behavioral-invariance
> discriminator and the `results_digest`-stable assumption are **gate-class-aware**:
> they apply only to the execution-deterministic class, so they no longer reject a
> legitimate statistical (visual-regression) or perceptual test. "Correctness is
> not one shape" — the deterministic oracle is one class, not the universal one.

### 1e. Change-risk tiering — assurance depth and HITL scale with risk

Not every change warrants the full nine-gate + signing + human-Release + admission
tax; making a one-line fix pay a major-feature's assurance cost is both
uneconomic and a HITL-throughput killer. Triage emits a **change-risk tier** (a
first-class, validated output — mis-tiering is caught the same way criteria/config
edits are, by deterministic pattern checks + HITL):

| Tier | Trigger (deterministic classifier) | Assurance depth | HITL |
|---|---|---|---|
| **Low** | No security-relevant path touched (no build/CI/IaC, deps/lockfile, scanner-config, auth/crypto, trust-root); bounded diff size | Reduced set (Build, Test on impacted criteria, scoped Security); **admission still fail-closed** | **Auto-approve Release**; spot-audited |
| **Standard** | Ordinary app-code feature/evolution | Full integrated gate set | Design + Release HITL |
| **High** | Touches build/CI/IaC, deps, scanner-config, auth/crypto, **or the trust root** | Full chain + Supply-chain + Threat-review | Design + **two-person** Release |

Any touch of a security-relevant or trust-root path forces **High** regardless of
size. The tier scales *assurance depth and human load*, never the *integrity*
guarantees (admission is always fail-closed; verdicts always runner-signed). The
**human-decisions-per-feature** metric ([evaluation.md §7](./evaluation.md)) keeps
tiering honest. *(COST-F2, SRE-F7)*

**Segregation of duties is an enforced predicate, not an assertion.** *(SEC-R12-F7)*
"Author != approver" is only safe if enforced: the system **records the author
identity(ies) per artifact**, and **admission/release REJECTS when an approver
identity intersects the author set** or lacks the **required distinct role**. The
**regulated pack** ([domains.md §4](./domains.md)) forces **two-person
distinct-identity sign-off generally** — not only on trust-root changes. Signer
**identities + roles are bound into the WORM record / admission predicate**, so the
distinctness check is itself re-derivable at admission, not relayed from agent
state.

> **Risk ≠ engineering blast radius in some domains.** A change's *regulatory
> significance* is orthogonal to its diff size — a one-line APR, dosing, or AML-
> threshold change is tiny but **reportable and re-validation-triggering**. A
> domain pack contributes a **regulatory-significance axis** that can
> **force-escalate** the tier and **force-on** human Release **independent of diff
> size**, and a pack may make **Autonomous mode and Low-tier auto-approve
> unreachable** entirely ([domains.md §4](./domains.md)) — enforced in core,
> fail-closed, so "Low risk → fewer humans" can never apply where the law requires
> a named accountable approver. *(Regulated-F1/F5, P-F4)*

### 1f. Re-verification is dependency-scoped, not total

A code change need not re-run *every* integrated gate from scratch. The
re-verification engine derives from the `tree_sha` delta **which gates the change
can affect** and re-runs only those; verdicts for unaffected inputs are **reused
from cache, keyed on the unchanged input sub-hash** (a cached PASS keyed on an
unchanged sub-tree/lockfile/config hash is still a *real* runner-attested verdict,
so the final attested gate-state still covers the whole tree). E.g. a pure
app-code change re-runs Test/E2E for the *impacted* criteria + SAST on changed
files but reuses the Audit/SBOM verdict if deps are unchanged; a lockfile change
re-runs Supply-chain + Audit but not unaffected app tests. The reuse key for
**execution-dependent (class-2) verdicts binds mutant-rotation-id +
scanner-rule-version** (the same values in the attestation tuple, §1c), so a
cached class-2 verdict is **invalidated across a rotation/rule-version boundary**
and re-grounds under the new rotation — a stale verdict's anti-gaming grounding
can never be laundered forward past a rotation. This turns the
worst-case re-verification *multiplier* (COST-F1) into a bounded,
change-proportional cost while preserving the whole-tree guarantee. A hard
**per-feature re-verification sub-budget** caps pathological cascades, with the
non-convergence bound as backstop. *(COST-F1)*

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
- **The class-4 verification trust store is pinned under the same regime.** The
  trust anchor that `verify_attestation` checks class-4 signatures against — the
  **IdP-federation config + PKI trust roots / CA list** — sits under the **same
  separate-trust-boundary, run-immutable, two-person-change-with-WORM-record** regime
  as scanner config and OIDC trust (§4); otherwise an attacker who can swap the trust
  store mints "valid" human attestations. Its **pinned trust-store revision hash is
  recorded in the SLSA predicate and re-checked at admission**, exactly as
  `scanner_config_revision_hash` is bound (§4–§5). *(SEC-R12-F5)*

### Execution plane
- All agent-written code runs in sandboxes (containers; Firecracker/gVisor for
  stronger isolation); never on the host.
- **Credentialed phase is separated from untrusted-code execution:** no
  agent-authored code or third-party install/`postinstall` script runs while task
  credentials are present. Dependencies are pulled through an **internal mirror
  of pre-vetted, hash-pinned artifacts**, so builds need no open egress. *(SEC4)*
  **The mirror does NOT share the model-provider proxy** — routing a tarball fetch
  through a proxy whose enforcement is model-schema/entropy-shaped is a confused
  deputy (wrong protocol, wrong rules). The mirror gets its **own dedicated
  per-destination proxy with its own enforcement** (hash-pin verification, no
  arbitrary host), kept **separate from the model-provider proxy**. *(SEC-R12-F3)*
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
  **Human-review volume is bounded** so mandatory diffs don't become a
  rubber-stamp: reachability-weighted prioritization (diff only what is actually
  imported/transitively reached), **diff-against-prior-vetted-version** rather than
  from-scratch where a prior version was vetted, a **per-feature new-dependency
  budget** that escalates as a design smell when exceeded, and an explicit policy
  that **obfuscated/minified/generated artifacts lacking buildable source are
  rejected, not hand-diffed**. *(SEC-R10-F2)*
- Egress is default-deny **through per-destination proxies with payload/size
  anomaly detection** — host allowlisting alone does not stop exfil over
  allowed channels (registry, git remote, model provider). The sandbox is treated
  as hostile. *(SEC4)*
- **Provider egress is a first-class exfil channel, not an allowlisted
  exception.** The adapter must reach its model provider over egress — a
  bidirectional, high-entropy channel anomaly-detection is structurally bad at
  policing — so **all adapter provider traffic routes through the mediating
  model-provider proxy** behind the **enforced adapter boundary**. The proxy (a) is
  the **only holder of the provider credential** (the adapter never holds it); (b)
  **injects the system prompt/SOP/tools and strips/audits smuggled content**; (c)
  **enforces that retrieved-context/secrets cannot appear in egressing request
  bodies** (content-scan + structural schema validation); and (d) **meters per-call
  entropy/size against the *task's* expected profile**, not a global baseline. The
  proxy and its content/structural enforcement are a **first-class red-team
  target**. *(SEC-R12-F3)*
  - **The proxy bounds but does not eliminate the exfil channel — state this
    honestly.** It is the same high-entropy bidirectional channel the plane concedes
    anomaly-detection is structurally bad at: a colluding adapter can encode
    secrets/context via lexical/ordering steganography inside legitimately-shaped
    prompt/code that passes every content/schema/entropy check. So the metering in
    (d) is a **detection signal feeding the deterministic monitor (§3), not a
    preventive boundary**, and **residual exfil bandwidth within a normal
    coding-task profile is an explicit open risk that must be quantified** (bits/call
    against the task's expected profile), not assumed closed. What actually
    *constrains* the channel are the **upstream data-plane controls**: scoped
    short-lived creds **never placed in prompt/state**, and the **no-third-party-
    model-egress profile for regulated data** — the proxy is defense-in-depth on top,
    not the boundary. **Red-team specifically for semantic/steganographic exfil
    within a normal coding-task profile**, not only for oversized/high-entropy bursts.

### Data plane
- Scoped, short-lived credentials per task; **never in prompts or state**;
  signing keys unreachable from any agent sandbox.
- **Deployment data-egress profiles (pack-set).** Redaction-before-egress is
  best-effort and **not** an acceptable PHI/PAN control on its own. A pack may
  select a **"no third-party model/trace egress"** profile (in-boundary/self-hosted
  models + trace backend) — with a **defined judge-decorrelation fallback** when
  only one approved model family is permitted (separated-context same-family +
  stronger oracle reliance + human review) — and a **"no real PHI/PAN in the
  pipeline"** posture enforced *upstream* via synthetic/de-identified test data,
  rather than redacting real regulated data at egress. *(Regulated-F4)*
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
- **Generated-code provenance:** a runner-computed similarity check flags
  verbatim/near-verbatim regurgitation of training data and copyleft-snippet
  contamination in the *generated code itself* (not just dependencies); matches
  route to HITL. AI contribution is recorded in the provenance/SLSA metadata. See
  [lifecycle.md §5](./lifecycle.md) for the licensing/liability posture. *(P-F5)*
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
    **Within-adapter-loop scope (mirror of architecture §2):** the semantic
    loop-signature + sampled analyzer extend to **adapter-TURN granularity**, keying
    on **trusted-infra-observed per-turn progress signals** (not adapter-emitted
    self-report), so **within-loop semantic non-convergence is detectable *before*
    the hard kill** — not only surfaced after the fact as a budget breach.
  - *Oracle / gate / criteria / scanner-config edits* — **any diff touching tests
    treated as oracles, gate logic, acceptance criteria, or scanner config is a
    *deterministic* trip** forcing HITL review (not an analyzer judgment). *(SEC3, AI10, SEC2)*
  - *Cost* — `BudgetLedger` thresholds; *Policy* — OPA deny-count;
    *Retry-until-green* — verdict oscillation.
  - *Pinned-constraint pressure* — the **total injected `pinned_constraints` set
    (global + scoped-local)** for a node approaching its injection budget escalates
    to HITL (never silent truncation). When the **global subset alone** approaches
    budget the condition is unfixable-by-scoping and escalates to governance/HITL
    to **retire or consolidate globals** ([architecture.md §8](./architecture.md)). *(AI-R8-F2, AI-R10-F2)*
  - *Criterion-coverage delta* — computed against signals the implementing agent
    does **not** solely control: the baseline is the **Spec-gate-frozen
    (immutable) criterion set**, and the satisfied side is the **count of
    high-risk `criterion_id`s whose runner-attested negative-oracle/mutant family
    passes** (computed by the trusted runner, §1c) — **not** the agent-authored
    test-tag count (which a coordinated scope-reduction could co-shrink). A drop
    in that runner-attested count against the frozen baseline trips deterministically.
    The **high-risk flag is an immutable attribute of each frozen criterion**, so
    the denominator cannot be shrunk by re-labeling (that would be an
    acceptance-criteria edit → a deterministic HITL trip). *(AI-R8-F3, AI-R9-F2, AI-R10-F3)*
- **Sandboxed LLM analyzer (semantic color only):** runs read-only in its **own
  toolless sandbox**, fired by (a) a deterministic-monitor signal, (b) a write to
  oracle/gate/criteria/config paths, **and (c) a low-rate *sampled* pass over a
  random subset of ordinary supersteps** — so semantic reward-hacking that stays
  under every deterministic bound and touches no protected path is still
  observed. It reads a **capped** slice of **deterministically-extracted
  structured features**, with untrusted trace prose delimited as *data, never
  instructions*. But framing prose as data stops instruction-injection, **not
  feature-flooding / cap-eviction** (flooding the capped slice so the suspicious
  feature is truncated out) **or feature-shaping** — and with the agent controlling
  the full transcript, every trace-derived feature is adversary-shaped. So analyzer
  features are derived **primarily from trusted-infra observations** (zone-B
  execution records, OPA decision logs, runner attestations), **not the
  agent-emitted transcript**, and **capped-slice selection is adversary-robust** —
  **priority-ranked by trusted signal, not recency/volume** — so flooding cannot
  evict the suspicious feature. It emits **advisory** flags only and cannot approve
  work or override a deterministic trip. Precision/recall — **including against
  prompt-injection-of-the-monitor, feature-flooding / cap-eviction, and on an
  *untriggered* held-out set** — is evaluated
  ([evaluation.md §4](./evaluation.md)); its sampled+triggered invocation count and
  token cost are sized into the per-feature cost model
  ([evaluation.md §8](./evaluation.md)) and metered as `monitor_overhead_usd`. *(SEC3, AI4, AI10, AI-R8-F3, SEC-R12-F4)*

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
- **Class-4 credential revocation, not just expiry.** Admission performs a
  **revocation-status check** (OCSP/CRL or IdP standing query) on **every class-4
  credential it relies on, at admission time** — a credential valid-and-unexpired at
  signing may have been **revoked** since. A **periodic re-validation sweep** re-checks
  standing class-4 attestations against current credential status; any credential
  **lapsed since signing** writes a **disqualifying WORM record + recall escalation**.
  This covers revocation specifically, beyond the expiry/withdrawal the signed
  `timestamp`/`meaning-of-signing` already capture. *(SEC-R12-F5)*
  - **Revocation-status-UNREACHABLE is FAIL-CLOSED.** A naive revocation check
    soft-fails when the responder is unreachable — the classic bypass: an attacker
    holding a compromised class-4 credential **DoSes the OCSP/CRL/IdP responder** so
    admission can't see the revocation, and "couldn't check" silently admits. So an
    **unreachable/indeterminate revocation status is treated as FAIL-CLOSED**: admission
    **does not admit**, and — to be unambiguous — this is **neither a gate `FAIL` nor an
    `error` verdict** (an `error` would route to bounded retry / never latch, §5/§10),
    but its **own admission-deny on indeterminate standing**. To avoid making admission
    hostage to responder liveness, this is satisfied offline by **OCSP stapling /
    short-lived signed standing assertions captured at sign-time and re-verified
    offline at admission** (no live responder call on the admission path) — a stapled
    proof that is missing or stale **also fails closed**. *(SEC-R12-F5)*
- **Fail-closed on *stable* FAILs, with a bounded recovery for transient ones.**
  Admission latches a digest as disqualified on a FAIL whose `results_digest`
  reflects a **stable, verdict-relevant input** (a config/scan-driven outcome — a
  newly-admitted CVE, a policy change, a deterministic re-derivation) — so two
  forks that legitimately diverge on the same digest can never ship the green one.
  A FAIL from **transient-execution divergence on an unchanged tree** (a flake
  exceeding the §1a N=2 quarantine budget) is **not** latched the same way: because
  the tree is genuinely correct, there is an explicit bounded recovery — a
  **quorum of trusted-runner re-runs that all PASS on the identical
  `tree_sha`+config**, **two-person admission override, WORM-logged**. `error`-status
  records (infra failure) never latch at all.
- **The latch is monotonic and WORM-history-scoped, not latest-record.** Admission
  evaluates the **full WORM history** for a digest: once a **stable FAIL** record
  exists for `D`, `D` is disqualified **until an explicit override**, regardless of
  any later PASS. A later recovery-quorum (or flaky) PASS **never auto-clears** a
  latched digest — note the live `gates` reducer (latest-by-`seq`) will legitimately
  show PASS and let in-graph routing proceed toward Release again, but **admission
  holds `D` latched** until the override; that store-level disagreement (routing
  liveness vs admission safety) is intentional. So the core policy reads: *all
  required gates PASS for `D` in WORM **and no un-overridden stable FAIL exists for
  `D`***. *(DS-R11-F1)*
- **`inconclusive` is an explicit verdict state for class-3.** The latch above
  distinguishes stable-FAIL from flake but has no state for **statistically
  inconclusive** — a class-3 (statistical/tolerance-band) verdict whose CI
  **straddles the threshold**, where two forks can legitimately land on opposite
  sides. So class-3 verdicts admit an explicit **`inconclusive`** state
  ([domains.md §1](./domains.md) owns the class-3 spec; this owns the
  admission-latch + look-ledger semantics) with defined routing — **collect more
  eval n, or HITL** — and **admission treats `inconclusive` fail-closed**, *not*
  flake-recoverable: a straddling CI is not a transient-execution divergence, so the
  bounded recovery-quorum path does **not** apply to it. *(SEC-R12-F8)*
  - **Repeated re-derivation is multiple testing — debit a look ledger.** A
    **class-3 look = ANY re-derivation of the class-3 decision against an eval set**,
    debited from a **look ledger** regardless of dependency-scoped reuse (§1f) — reuse
    does not exempt a fresh re-derivation from the ledger. `inconclusive`→**"collect
    more eval n"** is therefore an explicit **alpha-spending event** governed by a
    **pre-registered group-sequential boundary** with a **hard cap** on looks; silently
    re-running until the CI clears the threshold is exactly the optional-stopping attack
    this closes. **At the cap, `inconclusive` is TERMINAL → HITL / redesign, never
    auto-PASS.** The **look ledger + remaining alpha are bound into the signed class-3
    tuple** (alongside the eval-set/seed the runner re-derives from, §1c) and
    **re-checked at admission**, so a verdict minted past its look budget — or with a
    laundered ledger — is rejected exactly as a stale rotation/config binding is.
    - **The look ledger is a GLOBAL, cross-fork resource — forking does not reset
      class-3 alpha.** Within-run hardening is not sufficient: a checkpoint fork
      mints a **new `run_id`**, and a per-run ledger would silently hand each fork a
      fresh alpha budget — optional-stopping by forking until one fork's CI clears.
      So the ledger is **keyed on `(state_sha, metric/decision-id, eval-set
      identity)`, not on `run_id`**: every fork of the same class-3 decision
      **shares one alpha budget**, and a fork's look **debits the shared ledger**.
      Admission **rejects a class-3 verdict whose CUMULATIVE CROSS-FORK looks exceed
      the pre-registered group-sequential cap**, regardless of which `run_id`
      spent them. This places the class-3 look ledger in the same **GLOBAL /
      survive-forks** set as the other admission-latched records (the WORM stable-FAIL
      latch, §5; the cross-fork digest disqualification) — forking can never reset it.
- **Overrides are record-scoped, not digest-scoped.** A two-person override targets
  a **specific transient `(gate_name, tree_sha, results_digest)`** record (justified
  by its recovery quorum); admission then **re-evaluates the full gate set**, so a
  **co-resident stable FAIL** (e.g. an Audit/CVE FAIL on the same digest) remains
  latched. Clearing a flake can never inadvertently clear a genuine stable FAIL.
  This keeps the security guarantee for input-relevant FAILs while preserving
  liveness. *(SEC7, SEC5, SEC-R8-F1, DS-R9-F1, DS-R10-F1, DS-R11-F1, DS-R11-F2)*

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
- **Append-only/hash-chaining guarantees the container's *integrity*, not the
  content's *faithfulness*** — an agent can author misleading prose, or omit an
  event by simply not enqueuing it. So the store distinguishes **system-witnessed
  events** (emitted by non-agent components: runner attestations, admission
  decisions, policy denials, IdP-signed class-4 human attestations) from
  **agent-authored prose** — the latter is **tagged untrusted/advisory** in the
  record. **Critical audit events (verdicts, approvals, deploys, overrides) must be
  emitted by the trusted component that performed the act, *not* relayed from agent
  state**, so an agent can neither forge a system-witnessed event nor suppress one
  by withholding it. *(SEC-R12-F6)*

## 9. Domain packs

A Specialist is a conditional node implementing `{capabilities, tools,
prompts/SOP, gates, evals}`; a registry maps required capabilities to specialists.
**A pack is more than "a specialist + Audit checks"** — it may use the **core
hooks** in [domains.md §4](./domains.md) (verdict-class assignment, mode/tier veto,
regulatory-significance axis, per-artifact approver-of-record, data-egress profile,
rendered-evidence + perceptual HITL, `state_sha` inputs, expensive-gate
declaration, retention/identity policy). The table below is the *control-mapping*
slice only; the full per-domain specs (regulated, ML/data, frontend) are in
[domains.md §5](./domains.md).

| Pack | Adds specialist | Adds gates / controls |
|---|---|---|
| Fintech | Compliance specialist | PCI-DSS/SOX in Audit; stricter crypto review; data never leaves boundary |
| Healthcare | Privacy specialist | HIPAA mapping; PHI data-plane rules (no PHI to providers) |
| ML / Data | ML engineer | Data-validation + model-eval gate; dataset license checks |
| Mobile / Embedded | Platform specialist | Platform build/sign gates; resource budgets |

## 10. Failure & escalation policy

1. Gate `fail` → responsible role, increment retry counter.
2. Gate `error` (infra) → bounded per-branch retry → else escalate (no sibling loss).
3. Code change → new `tree_sha` → **dependency-scoped** re-verification: re-run the
   integrated gates the change can affect; reuse cached runner-attested verdicts
   for unaffected inputs (§1f).
4. Retry cap or **non-convergence bound** reached → Human Liaison (right diagnosis, not a budget breach).
5. Deterministic-monitor trip (loop / oracle-or-config edit / policy / cost) → pause + HITL; analyzer flags are advisory.
6. Budget breach → escalate (T1 never downgrades).
7. All escalations resume from the last checkpoint; effectful actions re-verify by idempotency key and never re-fire.
