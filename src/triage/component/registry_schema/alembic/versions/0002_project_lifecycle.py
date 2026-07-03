"""project lifecycle: dropped-project tombstone (ADR-0002 completion)

Revision ID: 0002_project_lifecycle
Revises: 0001_initial_registry_schema
Create Date: 2026-07-03

``triage project drop`` removes a project's database (``DROP DATABASE``,
ADR-0002 teardown) but keeps the registry row as an audit tombstone —
``registry.submissions`` foreign-keys to it, and "we had a project called X
until <date>" is control-plane history worth keeping. This migration adds the
``dropped`` status and its timestamp.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_project_lifecycle"
down_revision = "0001_initial_registry_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
        alter table registry.projects add column dropped_at timestamptz;
        alter table registry.projects drop constraint projects_status_check;
        alter table registry.projects add constraint projects_status_check
            check (status in ('active', 'archived', 'dropped'));
        """)


def downgrade():
    # A 'dropped' row can't survive the narrower check; fold it into 'archived'
    # (keeping the timestamp) rather than deleting audit history.
    op.execute(r"""
        update registry.projects
           set status = 'archived',
               archived_at = coalesce(archived_at, dropped_at)
         where status = 'dropped';
        alter table registry.projects drop constraint projects_status_check;
        alter table registry.projects add constraint projects_status_check
            check (status in ('active', 'archived'));
        alter table registry.projects drop column dropped_at;
        """)
