# Foundry application image — builds the `foundry` package and its deps with uv.
#
# SCAFFOLDING (TASK [14]): no docker daemon is available in the authoring
# environment, so this Dockerfile is NOT built/validated here. It is written to
# be EKS-portable by construction (12-factor: config via env, no baked-in
# secrets, runs as a non-root user). Build with: `docker build -t foundry:dev .`
#
# IMAGE PINNING: external base images SHOULD be pinned by digest for
# reproducibility/supply-chain (the `@sha256:...` form). The digests below are
# PLACEHOLDERS — resolve the real digest in your environment before shipping:
#     docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# and replace `@sha256:REPLACE_ME_*`. CI should fail if a tag is unpinned.

# --- builder: install deps + project into a self-contained venv --------------
# uv's official image bundles a pinned Python; tag pins the minor, digest pins
# the exact build (REPLACE the placeholder digest before shipping).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:REPLACE_ME_UV_BUILDER AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Layer-cache deps: copy only the lockfiles first. `--frozen` ensures the build
# matches the committed lock (no surprise resolution). `--no-install-project`
# installs only third-party deps so app-code changes don't bust this layer.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project || \
    uv sync --no-dev --no-install-project

# Now the source, then install the project itself into the same venv.
COPY foundry ./foundry
COPY README.md* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || uv sync --no-dev

# --- runtime: slim, non-root, venv-on-PATH -----------------------------------
# REPLACE the placeholder digest before shipping (see note above).
FROM python:3.12-slim-bookworm@sha256:REPLACE_ME_PYTHON_RUNTIME AS runtime

# Non-root by construction (EKS securityContext parity).
RUN groupadd --system foundry && useradd --system --gid foundry --home /app foundry

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 12-factor defaults; every one is overridable by the orchestrator (compose
    # env / K8s env). NO secrets baked in.
    FOUNDRY_CHECKPOINTER=sqlite \
    FOUNDRY_CAS_ROOT=/var/lib/foundry/cas \
    FOUNDRY_CHECKPOINT_DB=/var/lib/foundry/state/checkpoints.sqlite

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/foundry ./foundry
COPY pyproject.toml ./

# Volume mount points for the durable CAS + checkpoint DB (compose/K8s provide
# the actual volumes; these are owned by the non-root runtime user).
RUN mkdir -p /var/lib/foundry/cas /var/lib/foundry/state \
    && chown -R foundry:foundry /var/lib/foundry /app
USER foundry

# Smoke default: import the package. The real entrypoint is the cli, which this
# task does NOT touch — override `command:` in compose / K8s to run the factory.
ENTRYPOINT ["python", "-m"]
CMD ["foundry"]
