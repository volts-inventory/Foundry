# Foundry — Docker-Compose deployment profile (EKS-portable by construction)

This profile (`docker-compose.yml` + `Dockerfile`) runs Foundry as a small set
of containers whose every production concern maps 1:1 onto an EKS primitive, so
the same topology lifts to Kubernetes without rewriting app code.

> **What is validated vs scaffolding.** No docker daemon exists in the authoring
> environment, so the compose stack and the image are **NOT** built/run here —
> they are reviewable scaffolding. The one production seam that **IS** fully
> implemented and unit-tested is the durable filesystem CAS
> (`foundry/durability/fs_artifact_store.py`, `tests/test_fs_artifact_store.py`)
> and the checkpointer backend switch
> (`foundry/durability/checkpointer.py::make_checkpointer`,
> `tests/test_checkpointer_factory.py`). Everything in YAML/Dockerfile is marked
> SCAFFOLDING / STUB / PLACEHOLDER inline.

## Run it

```bash
# 1. Provide secrets via env (NEVER baked into the image). Compose reads .env.
cat > .env <<'EOF'
POSTGRES_PASSWORD=change-me
FOUNDRY_CHECKPOINTER=sqlite      # or 'postgres' once that swap is wired
EOF

# 2. Validate the compose file without starting anything:
docker compose config

# 3. Build + start:
docker compose up --build
```

Before shipping you must replace every `@sha256:REPLACE_ME_*` digest placeholder
in `Dockerfile` and `docker-compose.yml` with a real digest resolved in your
environment (`docker buildx imagetools inspect <image:tag>`). CI should fail on
any unpinned external image.

## 12-factor / portability discipline

The profile is built on three rules that make the EKS migration mechanical:

1. **State lives behind interfaces, on volumes — never in the app process.**
   - Artifact bodies/blobs go to the **content-addressed CAS**. The interface is
     `ArtifactStore` (in-memory, dev) and its drop-in durable twin
     `FilesystemArtifactStore` (same `put_body/put_body_at/get_body/put_blob/
     get_blob/resolve/resolve_blob` signatures), so `AgentRuntime.cas` swaps
     backends with no call-site changes. In compose it writes to the
     `foundry_cas` volume at `FOUNDRY_CAS_ROOT`.
   - Checkpoints go to the **checkpointer**, selected by env
     `FOUNDRY_CHECKPOINTER` (`sqlite` default, volume-backed; `postgres` for
     prod).
2. **Config and secrets arrive via the environment** (no hardcoded
   localhost/paths/credentials). Every endpoint and path in the compose file is
   an env var with a safe default; required secrets use `${VAR:?msg}` so
   startup fails closed when one is missing.
3. **Privileged work is network-isolated into separate services.** The proxy
   (sole model-provider egress chokepoint), signer (Zone-B key material), and
   builder (sandboxed diff execution) are distinct containers on isolated
   networks — never sharing a process with app code.

## EKS migration note

| Compose (this profile) | EKS target | How |
| --- | --- | --- |
| `foundry_cas` volume (`FilesystemArtifactStore`) | **S3** object store | Add an `S3ArtifactStore` behind the SAME `ArtifactStore` interface (write the two seams against a bucket instead of a directory); point `AgentRuntime.cas` at it. Drop-in — no call-site change. Pod identity via **IRSA**. |
| `postgres` service + `pg_data` volume | **RDS** (managed Postgres) | Keep the `FOUNDRY_POSTGRES_DSN` contract; point it at the RDS endpoint. Finish wiring `make_checkpointer`'s `postgres` branch (see below). |
| Secrets in `.env` / env vars | **K8s Secrets**, mounted as env or files; cloud creds via **IRSA** | The provider API key mounts into the *proxy* Secret, the signing key into the *signer* Secret — never the app. |
| `networks: { internal: true }` + service split | **NetworkPolicy** + per-service **ServiceAccount** | One Deployment per service; NetworkPolicy denies app→provider egress except via the proxy; builder gets a sandboxed `runtimeClass` (gVisor/Kata). |
| `restart: unless-stopped`, single app replica | **Deployment replicas + HPA** | The app is stateless (all state is behind the CAS/checkpointer interfaces), so it scales horizontally; an HPA scales on CPU/queue depth. |
| Commented OTel/Prometheus/Grafana/Loki | Helm: **OTel-Collector + kube-prometheus-stack + Loki** | Scrape the app `/metrics` + OTLP exporters. Left as a placeholder here on purpose (not real infra). |

### The Postgres checkpoint swap (documented swap point)

`make_checkpointer` reads `FOUNDRY_CHECKPOINTER`. `sqlite` is fully wired and
tested; `postgres` currently **fails closed** with a pointer here, because the
`PostgresSaver` must join the *identical* one-transaction-per-superstep
discipline the SQLite backend enforces via `_SuperstepConnection` (architecture
§5 / DS3): a `PostgresSaver` constructed on a shared connection whose `commit()`
defers to `superstep_txn`, so the checkpoint, outbox rows, ledger updates and
turn-snapshot handle all commit in one server-side transaction or none do.
Wiring and validating that requires a live Postgres, which is unavailable in the
authoring environment — hence it is a documented swap point, not fabricated
untestable code. To complete it: `uv sync --extra postgres`, build the
shared-connection `PostgresSaver` path, and add tests against a real Postgres.

## What this task does NOT do (honesty)

- Does **not** build the real proxy / signer / builder services — they are
  network-isolated **stubs** (`command: ["true"]`) to make the topology
  reviewable.
- Does **not** stand up the observability stack — it is a commented placeholder.
- Does **not** touch `cli.py`, `proxy/`, `gates/`, or `agents/`; the app
  `command:` in compose is left commented for whoever wires the cli entrypoint.
