"""initial triage-pg registry control-plane schema (ADR-0002)

Revision ID: 0001_initial_registry_schema
Revises:
Create Date: 2026-06-16

Greenfield baseline for the *registry* control-plane database (ADR-0002):
creates the ``registry`` schema exactly as specified in
``docs/schema-design.md`` §3. The registry is a single instance-wide control
plane — projects, users, membership, and an append-only submission audit
trail — distinct from the per-project ``triage`` results schema, which the
greenfield baseline (``0001_initial_triage_schema``) deliberately scoped out.

The registry holds **no DB credentials**: cloud uses an IAM role per project,
local uses env (ADR-0004).

Written as raw SQL on purpose, mirroring the results baseline: enums, check
constraints, and ``gen_random_uuid()`` defaults read most clearly as DDL.
Requires PostgreSQL >= 13 (``gen_random_uuid()`` is built in).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial_registry_schema"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA_DDL = r"""
create schema if not exists registry;

-- ------------------------------------------------------------- projects
-- Each project is one isolated PostgreSQL database in the shared cluster
-- (ADR-0002). NO credentials: cloud -> IAM role per project; local -> env
-- (ADR-0004).
create table registry.projects (
    project_id    uuid primary key default gen_random_uuid(),
    slug          text not null unique,        -- url-safe; also the per-project DB name
    display_name  text not null,
    database_name text not null unique,        -- target DB in the shared cluster
    status        text not null default 'active'
                    check (status in ('active', 'archived')),
    created_at    timestamptz not null default now(),
    archived_at   timestamptz
);

-- --------------------------------------------------------------- users
create table registry.users (
    user_id      uuid primary key default gen_random_uuid(),
    email        text not null unique,
    display_name text,
    is_admin     boolean not null default false,
    created_at   timestamptz not null default now()
);

-- ----------------------------------------------------- project_members
create table registry.project_members (
    project_id uuid not null references registry.projects(project_id) on delete cascade,
    user_id    uuid not null references registry.users(user_id)       on delete cascade,
    role       text not null default 'contributor'
                 check (role in ('owner', 'contributor', 'viewer')),
    added_at   timestamptz not null default now(),
    primary key (project_id, user_id)
);

-- --------------------------------------------------------- submissions
-- Append-only audit trail: who submitted what, where it was routed.
create table registry.submissions (
    submission_id   uuid primary key default gen_random_uuid(),
    project_id      uuid not null references registry.projects(project_id) on delete cascade,
    submitted_by    uuid references registry.users(user_id),
    experiment_hash text,                       -- maps to triage.experiments in the project DB
    profile         text not null default 'local'
                      check (profile in ('local', 'cloud')),
    batch_job_id    text,                        -- AWS Batch id (cloud), null for local
    submitted_at    timestamptz not null default now()
);
"""


def upgrade():
    op.execute(SCHEMA_DDL)


def downgrade():
    # The schema owns its tables; cascade drops everything.
    op.execute("drop schema if exists registry cascade;")
