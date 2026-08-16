"""upload session project/pair foreign keys set null on delete

Revision ID: d4e8b2c6a0f3
Revises: c3f7a1d5e9b4
Create Date: 2026-08-16 00:10:00.000000

Wave 1 P0-5. `upload_sessions.project_id` / `pair_id` referenced projects/
project_pairs with PostgreSQL's default NO ACTION and nothing ever cleared
them, so deleting a project that had been created through the resumable-upload
flow raised IntegrityError in production. SQLite does not enforce foreign keys
by default, which is why the entire test suite passed.

RETENTION DECISION: UploadSession rows are operational/audit history - they
carry failure_code, byte offsets, checksums and timing that are the only record
of how an upload behaved, and they are read by the Admin upload-diagnostics
panel. They are therefore NOT cascade-deleted with the project. The references
are nulled instead (ON DELETE SET NULL), which is why both columns were already
declared nullable.

This is enforced at the DATABASE level rather than only in the delete helper so
that every path is covered - including the ORM cascades from Admin.projects and
User, which bypass the helper entirely.
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e8b2c6a0f3"
down_revision = "c3f7a1d5e9b4"
branch_labels = None
depends_on = None


FKS = (
    ("fk_upload_sessions_project_id_projects", "project_id", "projects"),
    ("fk_upload_sessions_pair_id_project_pairs", "pair_id", "project_pairs"),
)


def _rebuild(ondelete):
    # Existing constraints were created unnamed by the original revision, so the
    # server-assigned name differs per backend. Reflect and drop by real name.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {}
    for fk in inspector.get_foreign_keys("upload_sessions"):
        if fk.get("constrained_columns"):
            existing[fk["constrained_columns"][0]] = fk.get("name")

    with op.batch_alter_table("upload_sessions") as batch_op:
        for new_name, column, referred in FKS:
            current = existing.get(column)
            if current:
                try:
                    batch_op.drop_constraint(current, type_="foreignkey")
                except Exception:
                    pass
            batch_op.create_foreign_key(
                new_name, referred, [column], ["id"], ondelete=ondelete
            )


def upgrade():
    _rebuild("SET NULL")


def downgrade():
    _rebuild(None)
