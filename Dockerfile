# syntax=docker/dockerfile:1
#
# triage-pg image — greenfield architecture (ADR-0001/0003/0005/0008).
#
# Build (BuildKit + SSH forwarding required: the `featurizer` dependency is a
# private git+ssh dependency, ADR-0008/0016 — pinned at
# pyproject.toml as `featurizer[parquet] @ git+ssh://git@github.com/.../featurizer.git@vX`):
#
#     DOCKER_BUILDKIT=1 docker build --ssh default -t triage-pg:dev .
#
# `--ssh default` forwards the host SSH agent into the `RUN --mount=type=ssh`
# steps so `uv sync` can clone the private featurizer repo. The host must have
# the featurizer deploy key loaded in its agent first (`ssh-add <key>`); verify
# with `ssh-add -l`. No key is ever copied into the image or a layer.
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
# git + openssh-client so uv can fetch the private featurizer git+ssh dependency.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        libpq5 \
        openssh-client \
        postgresql-client && \
    rm -rf /var/lib/apt/lists/*

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
# --mount=type=ssh forwards the host agent; uid/gid=1000 makes the forwarded
# agent socket readable by the non-root `triage` user (uv's git fetch runs as
# that user — without this the clone fails "Permission denied (publickey)").
# ssh-keyscan seeds github.com so the git+ssh clone doesn't fail host-key
# verification. Errors are NOT swallowed.
RUN --mount=type=ssh,uid=1000,gid=1000 \
    mkdir -p -m 0700 /home/triage/.ssh && \
    ssh-keyscan -t rsa,ed25519 github.com >> /home/triage/.ssh/known_hosts && \
    uv sync --frozen --no-dev --no-editable

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
# uid/gid=1000 so the forwarded agent socket is readable by the triage user
# (see the base stage's uv sync note).
RUN --mount=type=ssh,uid=1000,gid=1000 \
    uv sync --frozen --extra dev

# Like base: no ENTRYPOINT so any argv (triage …, pytest …, bash) runs as-is.
# A shell is the natural default for an interactive dev container.
#   docker run --rm triage-pg:dev triage --help   -> CLI help (overrides CMD)
#   docker run --rm -it triage-pg:dev              -> shell
CMD ["/bin/bash"]
