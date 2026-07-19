# syntax=docker/dockerfile:1
#
# triage-pg image — greenfield architecture (ADR-0001/0003/0005/0008).
#
# Build (no secrets: the `featurizer` dependency is a public git+https pin on
# ccd-ia/featurizer, ADR-0008/0016 — see pyproject.toml):
#
#     docker build -t triage-pg:dev .
#
# Stages:
#   base        — runtime image: package + `triage` CLI + shell, non-root user.
#                 dirtyduck/docker-compose.yml's bastion targets this stage.
#   development — base + dev extras (pytest, ruff, basedpyright, …) for tooling.

# ---------------------------------------------------------------------------
# base — runtime: greenfield package installed via uv sync (no dev extras).
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

LABEL org.opencontainers.image.title="triage-pg" \
      org.opencontainers.image.description="PostgreSQL-native temporal ML pipeline (greenfield fork of dssg/triage)" \
      maintainer="Adolfo De Unánue <adolfo+claude@unanue.mx>" \
      triage.stage="base"

# Runtime deps: libpq for psycopg, postgresql-client for psql in the bastion,
# git so uv can fetch the featurizer git+https dependency.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        libpq5 \
        postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Amazon RDS global CA bundle — CloudAuth forces sslmode=verify-full for IAM-token connections
# (ADR-0004), so the container must ship the RDS root chain at the path
# triage.profiles.auth._DEFAULT_RDS_CA_BUNDLE expects. The all-regions bundle validates any RDS
# endpoint; refreshed periodically by AWS, so rebuild picks up rotations.
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    /etc/ssl/certs/rds-combined-ca-bundle.pem

# Non-root runtime user (Docker hard rule: the image must not force root).
# Compose authors mount host paths with --user $(id -u):$(id -g); this default
# user is for non-mounted / interactive use.
ARG TRIAGE_UID=1000
ARG TRIAGE_GID=1000
RUN groupadd --gid "${TRIAGE_GID}" triage && \
    useradd --uid "${TRIAGE_UID}" --gid "${TRIAGE_GID}" \
        --create-home --home-dir /home/triage --shell /bin/bash triage

# uv configuration: install into a project-local .venv and copy (not symlink)
# packages so the venv is self-contained.
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/triage/.venv \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/triage/.venv \
    PATH="/opt/triage/.venv/bin:${PATH}" \
    SHELL=/bin/bash

# WORKDIR creates /opt/triage owned by root; hand it to the runtime user so the
# unprivileged `uv sync` below can create .venv inside it.
RUN mkdir -p /opt/triage && chown triage:triage /opt/triage
WORKDIR /opt/triage

# Dependency metadata first so the heavy `uv sync` layer caches across source
# edits. uv.lock pins the exact featurizer commit (ADR-0016).
COPY --chown=triage:triage pyproject.toml uv.lock ./
COPY --chown=triage:triage README.md LICENSE ./
COPY --chown=triage:triage src/ src/

USER triage

# Install the locked dependency closure (no dev extras for the runtime image).
# The featurizer pin clones over public https — no agent forwarding, no keys.
RUN uv sync --frozen --no-dev --no-editable

# No ENTRYPOINT: the bastion (dirtyduck/docker-compose.yml, target: base) needs a
# plain command surface so `triage …`, `psql …`, and an interactive shell all
# work as a literal argv. Default command prints CLI help.
#   docker run --rm triage-pg:dev triage --help   -> CLI help
#   docker run --rm -it triage-pg:dev bash         -> shell
CMD ["triage", "--help"]

# ---------------------------------------------------------------------------
# development — base + dev extras (pytest, ruff, basedpyright, …) + editable
# install, for running the test suite / tooling inside the container.
# ---------------------------------------------------------------------------
FROM base AS development

LABEL triage.stage="development"

USER triage
WORKDIR /opt/triage

# Re-sync including the dev extra; editable so a bind-mounted src/ is live.
RUN uv sync --frozen --extra dev

# Like base: no ENTRYPOINT so any argv (triage …, pytest …, bash) runs as-is.
# A shell is the natural default for an interactive dev container.
#   docker run --rm triage-pg:dev triage --help   -> CLI help (overrides CMD)
#   docker run --rm -it triage-pg:dev              -> shell
CMD ["/bin/bash"]

# ---------------------------------------------------------------------------
# frontend-build — compile the React + Vite SPA (frontend/) to static assets.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend-build

WORKDIR /frontend
# Lockfile first so `npm ci` layer-caches across source-only edits.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build      # -> /frontend/dist

# ---------------------------------------------------------------------------
# dashboard — read-only dashboard runtime: base + the `dashboard` extra
# (fastapi/uvicorn) + the built SPA bundle, served by uvicorn (ADR-0012/0021).
# Build:  docker build --target dashboard -t triage-pg:dashboard .
# Run:    docker run --rm -p 8000:8000 -e PGHOST=… -e PGPORT=… -e PGUSER=… \
#                -e PGPASSWORD=… -e PGDATABASE=… triage-pg:dashboard
# ---------------------------------------------------------------------------
FROM base AS dashboard

LABEL triage.stage="dashboard"

USER triage
WORKDIR /opt/triage

# Add the web deps on top of base's runtime closure (featurizer already installed).
# fastapi/uvicorn/httpx are PyPI.
RUN uv sync --frozen --no-dev --no-editable --extra dashboard

# Drop the Vite bundle at a fixed path and point the app at it via TRIAGE_DASHBOARD_STATIC,
# so static serving does not depend on the (--no-editable) package install layout.
COPY --from=frontend-build --chown=triage:triage /frontend/dist /opt/triage/dashboard-static
ENV TRIAGE_DASHBOARD_STATIC=/opt/triage/dashboard-static

EXPOSE 8000
# Project DB comes from PG*/DATABASE_URL at runtime (never baked into the image).
# No ENTRYPOINT (base): override the CMD to get a shell/triage CLI if needed.
CMD ["uvicorn", "triage.dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
