"""project public key

Revision ID: a4f2c8d9e1b7
Revises: f6a8d0c2e4b9
Create Date: 2026-08-19 18:10:00.000000

Adds an opaque, immutable public key for Project. Existing rows are backfilled
with random token_urlsafe keys; no key is derived from Project.id.
"""
from alembic import op
import sqlalchemy as sa
import secrets


revision = "a4f2c8d9e1b7"
down_revision = "f6a8d0c2e4b9"
branch_labels = None
depends_on = None


TABLE = "projects"
INDEX = "ix_projects_public_key"
UNIQUE = "uq_projects_public_key"


def _generate_project_key(existing):
    for _ in range(20):
        key = f"prj_{secrets.token_urlsafe(16).rstrip('=')}"
        if key not in existing:
            existing.add(key)
            return key
    raise RuntimeError("Could not generate unique Project public_key")


def upgrade():
    bind = op.get_bind()
    op.add_column(TABLE, sa.Column("public_key", sa.String(length=64), nullable=True))

    rows = bind.execute(sa.text(f"SELECT id FROM {TABLE} WHERE public_key IS NULL ORDER BY id")).fetchall()
    existing = {
        row[0] for row in bind.execute(sa.text(f"SELECT public_key FROM {TABLE} WHERE public_key IS NOT NULL")).fetchall()
    }
    for row in rows:
        bind.execute(
            sa.text(f"UPDATE {TABLE} SET public_key = :public_key WHERE id = :id"),
            {"public_key": _generate_project_key(existing), "id": row.id},
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.alter_column("public_key", existing_type=sa.String(length=64), nullable=False)
            batch_op.create_unique_constraint(UNIQUE, ["public_key"])
    else:
        op.alter_column(TABLE, "public_key", existing_type=sa.String(length=64), nullable=False)
        op.create_unique_constraint(UNIQUE, TABLE, ["public_key"])
    op.create_index(INDEX, TABLE, ["public_key"], unique=False)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.drop_index(INDEX, table_name=TABLE)
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.drop_constraint(UNIQUE, type_="unique")
            batch_op.drop_column("public_key")
    else:
        op.drop_index(INDEX, table_name=TABLE)
        op.drop_constraint(UNIQUE, TABLE, type_="unique")
        op.drop_column(TABLE, "public_key")
