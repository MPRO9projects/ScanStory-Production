"""content report survives project delete (V1.1 production-ops)

Revision ID: e9b4d7a2c815
Revises: c1a7f3d95e24
Create Date: 2026-08-18 09:00:00.000000

WHY
`content_reports.project_id` was NOT NULL with an unnamed, plain FK to
projects.id (PostgreSQL default NO ACTION), and the ORM relationship carried
`cascade="all, delete-orphan"`. The cascade - not the database - was doing the
damage: the superadmin hard-delete path deleted every moderation report filed
against a project along with the project. The record of WHY content was removed
disappeared with the content, which is an audit-integrity gap: with a NOT NULL
FK there is simply no representation for "report whose project is gone", so the
row had to either cascade away or block the delete.

RETENTION DECISION - the same one taken for UploadSession (P0-5) and for
ownership history (P0-2): a ContentReport is moderation evidence, not project
debris. It carries the reporter's identity (or their deliberate anonymity), the
reason, the reviewing admin, the resolution and its timestamps, and those are
the only record of a moderation decision. It is therefore detached and kept,
never deleted.

WHAT
1. `content_reports.project_id` becomes nullable.
2. The unnamed FK is replaced by `fk_content_reports_project_id_projects` with
   ON DELETE SET NULL.

Enforced at the DATABASE level, not in the delete helper, so every path is
covered - including the ORM cascades from Admin.projects / User and any raw
DELETE that never touches the helper.

Non-destructive: no row is deleted, no column is dropped or retyped, no value is
reset. A report that currently points at a live project keeps that project_id
unchanged; only future deletions detach. Indexes are untouched
(ix_content_reports_project_id and ix_content_reports_project_status both remain
useful - a detached row simply sorts under NULL).

DOWNGRADE re-tightens project_id to NOT NULL and restores NO ACTION. That is
deterministic only while no detached rows exist, which is why it REFUSES rather
than guesses: rows with project_id IS NULL would have to be either deleted or
re-pointed at a project that no longer exists, and both destroy moderation
history. Preserving the evidence beats a conveniently succeeding downgrade.
Mirrors c1a7f3d95e24's downgrade guard.
"""
from alembic import op
import sqlalchemy as sa


revision = "e9b4d7a2c815"
down_revision = "c1a7f3d95e24"
branch_labels = None
depends_on = None


TABLE = "content_reports"
FK_NAME = "fk_content_reports_project_id_projects"


def _existing_project_fk_name():
    # The original revision (a1c3e5b7d9f2) created the constraint inline via
    # sa.ForeignKey, so it is unnamed and its server-assigned name differs per
    # backend (PostgreSQL calls it content_reports_project_id_fkey). Reflect and
    # drop by real name, exactly as d4e8b2c6a0f3 does.
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(TABLE):
        if fk.get("constrained_columns") == ["project_id"] and fk.get("name"):
            return fk["name"]
    return None


def _reshape(nullable, ondelete):
    """Set project_id's nullability and rebuild its FK with `ondelete`.

    PostgreSQL supports DROP CONSTRAINT / ALTER COLUMN natively, so it gets plain
    operations and the table is never recreated. SQLite cannot alter a column's
    nullability at all, so only it falls back to batch mode's table copy.
    """
    if op.get_bind().dialect.name == "sqlite":
        # copy_from omits the project_id FK, so there is nothing to drop: the
        # recreated table simply gets the new one. Without that, the old rule
        # would survive the copy alongside the new one, leaving two conflicting
        # ON DELETE rules on the same column.
        with op.batch_alter_table(TABLE, copy_from=_reflected_table()) as batch_op:
            batch_op.alter_column(
                "project_id",
                existing_type=sa.Integer(),
                nullable=nullable,
                existing_nullable=not nullable,
            )
            batch_op.create_foreign_key(FK_NAME, "projects", ["project_id"], ["id"], ondelete=ondelete)
        return

    current = _existing_project_fk_name()
    if current:
        op.drop_constraint(current, TABLE, type_="foreignkey")
    op.alter_column(
        TABLE,
        "project_id",
        existing_type=sa.Integer(),
        nullable=nullable,
        existing_nullable=not nullable,
    )
    op.create_foreign_key(FK_NAME, TABLE, "projects", ["project_id"], ["id"], ondelete=ondelete)


def upgrade():
    _reshape(nullable=True, ondelete="SET NULL")


def downgrade():
    detached = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE project_id IS NULL")  # noqa: S608 - fixed literal table name
    ).scalar()
    if detached:
        raise RuntimeError(
            f"Refusing to downgrade: {TABLE} has {detached} detached moderation "
            "report(s) with project_id IS NULL. Re-tightening the column would "
            "require deleting the record of why that content was reported. "
            "Resolve those rows deliberately first."
        )

    _reshape(nullable=False, ondelete=None)


def _reflected_table():
    """Reflect content_reports with the project_id foreign key stripped out.

    SQLite batch mode recreates the table from a reflected definition. Left in,
    the old unnamed NO ACTION constraint would be carried into the new table
    alongside the SET NULL one, leaving two conflicting delete rules on the same
    column (SET NULL and a restrict), which makes the delete fail outright once
    PRAGMA foreign_keys=ON. Only that one constraint is removed; every column,
    type, default, index and other FK is reflected as-is, so nothing else changes
    and no data is lost. Ignored entirely on PostgreSQL, which alters in place.
    """
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=op.get_bind())
    stale = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
        and [c.name for c in constraint.columns] == ["project_id"]
    ]
    for constraint in stale:
        table.constraints.discard(constraint)
        for fk in list(constraint.elements):
            table.c.project_id.foreign_keys.discard(fk)
    return table
