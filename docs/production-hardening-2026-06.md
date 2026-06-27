# Foundry — Production-Hardening Pass (2026-06)

This pass took the dev-grade-but-honest codebase toward production grade. The guiding
rule was the repo's own principle: **ship nothing untestable** — every change below is
validated (full suite green throughout: 1867 passing, ruff + mypy clean), and anything
that genuinely requires external infrastructure is documented, not faked.

## Done & validated

### Trust root — the cryptographic core
- **Asymmetric signing root (Ed25519).** The trust root was symmetric HMAC only: the
  party that *verifies* a verdict held the same secret that *signs* it, so anyone who
  could verify could forge. Added a pluggable `SigningScheme`
  (`foundry/gates/signing.py`): HMAC stays the byte-identical dev default; `ed25519`
  is a real asymmetric root where the verifier holds **only the public key and cannot
  sign**. Selected by `FOUNDRY_SIGNING_SCHEME=ed25519` + key env, fail-closed on
  misconfig. **Validated end-to-end**: a full autonomous run passes under ed25519 with
  the verifier holding only the public key. A hardware HSM/KMS/keyless-OIDC root is the
  next swap and slots in behind the same interface.

### Trust-claim audit — properties that were *documented but inert*
A systematic audit (the H3 pattern: a check that reads a ledger nobody writes) found
six more. Wired the highest-value ones:
- **Single-use nonce + negative-verdict latch (H3, prior).** Now live: per-request
  nonce issuance + a trusted commit step.
- **Cross-fork forged-verdict disqualification (audit F1).** A forgery/tamper admission
  DENY now `disqualify_digest`s the digest, so a sibling fork can't re-present it —
  previously only `red_team` ever wrote that ledger. (Honest gate *failures* are
  excluded; the stable-FAIL latch owns those.)
- **Pinned-model drift fails closed (audit F3).** The CLI built the proxy with the
  ADVISORY default, so a served-model drift on a gate/judgment call was logged but never
  halted. Now `FAIL_CLOSED` by default (`FOUNDRY_DRIFT_POLICY=advisory` to opt out).

### Operability
- **Webapp auth (W2).** State-changing routes now enforce a same-origin/CSRF check
  (a malicious page can't POST to a localhost workshop) plus an optional shared token
  (`FOUNDRY_WORKSHOP_TOKEN`) for LAN exposure. The UI forwards `?token=…`.
- (Prior in this branch) durable state on by default + crash-resume, multi-device live
  log, ledger-concurrency hardening (`UNIQUE(prev_hash)`, atomic `consume_nonce`).

### Persistence (Postgres HA) — swap point de-risked
- The Postgres checkpointer was a `NotImplementedError` ("can't validate without a live
  PG"). Stood up a live PG16 and **validated the hard unknowns**
  (`tests/test_postgres_checkpointer.py`): `PostgresSaver` round-trips, and crucially
  `PostgresSaver.put()` **respects the outer transaction** (no self-commit) — so the
  one-txn-per-superstep DS3 discipline ports *more simply* than SQLite. Kept fail-closed
  pending the one remaining scoped step (below).

## Remaining for full production — and *why* (honest boundaries)

These are not hand-waving; each is blocked on a specific thing this sandbox cannot
provide, or is a scoped change deliberately deferred to a separately-reviewed PR.

| Item | Status | Blocker / scope |
|---|---|---|
| **Postgres outbox/WORM SQL** | scoped, fail-closed | A dialect shim (`%s` params, IDENTITY cols) so the outbox shares the saver's psycopg connection (DS3). The hard part (atomicity model) is validated; this is a contained, separately-reviewed change to trust-critical SQL. |
| **Hardware crypto root (HSM/KMS/Sigstore)** | interface ready | Ed25519 (software key) is wired; a hardware/keyless root needs real KMS/OIDC infra and slots behind `SigningScheme`. |
| **Physical zone isolation** | logical-only | The two-zone seal (`seal_key`/`pinned_config`) only matters once Zone A/B are separate containers; needs a running container/K8s runtime (no docker daemon here). In one process the attack is unreachable. |
| **Real scanners (OPA/Semgrep/Syft)** | seam ready | Need the scanner binaries + a scanning execution adapter. |
| **Held-out-mutant discriminator live (audit F4)** | seam ready | Needs per-build mutant generation against the agent's own code+tests (a mutation engine), not a fixed set. |
| **Class-3 alpha-look grounding (audit F2)** | seam ready | ML-eval-specific: needs the statistical draw wired into an ml_data build (the ledger is never written on a normal run). |
| **Negative-latch release (audit F6)** | fail-safe | `release_negative_verdict` has no caller — a latched review can't be cleared in-run (conservative/safe, not a bad-release risk). |
| **S3 CAS / EKS** | interface ready | `FilesystemArtifactStore` is the durable seam; S3 + EKS need real AWS. |

## Bottom line
The **trust model is now substantially more real**: an asymmetric root the verifier
can't forge, the cross-fork forgery defense and drift-fail-closed actually live, and the
webapp guarded. The remaining gaps are infrastructure swaps behind validated interfaces
(Postgres dialect, HSM, containers, scanners) — not rewrites, and each with the hard
unknowns now resolved.
