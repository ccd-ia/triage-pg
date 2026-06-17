"""Registry control-plane database schema (ADR-0002).

The *registry* is a distinct control-plane PostgreSQL database — separate from
the per-project ``triage`` results schema (see
``triage.component.results_schema``). It holds the multi-tenant control plane:
projects, users, membership, and an append-only submission audit trail (the
authoritative table/column shapes live in ``docs/schema-design.md`` §3).

This package mirrors ``results_schema`` but maintains its **own** alembic
history with a dedicated version table (``registry_schema_versions``), so the
two migration lineages never collide. Run it with::

    just alembic-registry upgrade head

It deliberately carries no SQLAlchemy ``Base`` / model metadata: like the
greenfield results baseline, the migration is raw-SQL DDL, so there is nothing
to autogenerate against.
"""
