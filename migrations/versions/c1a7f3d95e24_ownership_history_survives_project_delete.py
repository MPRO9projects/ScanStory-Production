"""ownership history survives project delete (V1.1 P0-2)

Revision ID: c1a7f3d95e24
Revises: a9d3c7e1b502
Create Date: 2026-08-17 10:00:00.000000

WHY
project_ownership_transfers.project_id and project_ownership_claims.project_id
were NOT NULL with a plain FK to projects.id and no cascade rule. Deleting a
project therefore blew up with a NOT NULL violation (reproduced at runtime:
"NOT NULL constraint failed: project_ownership_transfers.project_id") for ANY
project that had ever been transferred or claimed - including projects whose
only history is COMPLETED or CANCELLED.

The two ways out were cascade-delete (destroys the audit trail) or detach-and-
keep. This picks detach-and-keep: the row is ownership evidence and outlives
the project it describes.

WHAT
1. project_id becomes nullable on both tables.
2. historical_project_id (indexed int) + historical_project_name (varchar 255)
   are added to both tables, populated by the application at delete time, so a
   detached row stays queryable by project id and readable by a human with no
   projects row left to join to.

Additive and non-destructive: no row is deleted, no column is dropped or
retyped, no backfill is invented (live rows keep project_id and read as
historical_* NULL, which is the truth - they were never detached).

DOWNGRADE re-tightens project_id to NOT NULL and drops the two columns. It is
deterministic only while no detached rows exist, which is why it refuses rather
than guesses: rows with project_id IS NULL would have to be either deleted or
re-pointed at a project that no longer exists, and both are data loss.
"""
from alembic import op
import sqlalchemy as sa


revision = "c1a7f3d95e24"
down_revision = "a9d3c7e1b502"
branch_labels = None
depends_on = None


TABLES = ("project_ownership_transfers", "project_ownership_claims")
NEW_COLUMNS = ("historical_project_id", "historical_project_name")


def _columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    for table in TABLES:
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "project_id",
                existing_type=sa.Integer(),
                nullable=True,
                existing_nullable=False,
            )
            if "historical_project_id" not in existing:
                batch.add_column(sa.Column("historical_project_id", sa.Integer(), nullable=True))
            if "historical_project_name" not in existing:
                batch.add_column(sa.Column("historical_project_name", sa.String(length=255), nullable=True))
        if "historical_project_id" not in existing:
            op.create_index(
                f"ix_{table}_historical_project_id",
                table,
                ["historical_project_id"],
            )


def downgrade():
    bind = op.get_bind()
    for table in TABLES:
        detached = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE project_id IS NULL")  # noqa: S608 - fixed literal table names
        ).scalar()
        if detached:
            raise RuntimeError(
                f"Refusing to downgrade: {table} has {detached} detached audit row(s) "
                "with project_id IS NULL. Re-tightening the column would require "
                "deleting ownership evidence. Resolve those rows deliberately first."
            )

    for table in TABLES:
        existing = _columns(table)
        if "historical_project_id" in existing:
            op.drop_index(f"ix_{table}_historical_project_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            for column in NEW_COLUMNS:
                if column in existing:
                    batch.drop_column(column)
            batch.alter_column(
                "project_id",
                existing_type=sa.Integer(),
                nullable=False,
                existing_nullable=True,
            )
