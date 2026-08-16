"""addon catalog type check allows project service coverage

Revision ID: c3f7a1d5e9b4
Revises: b2c4d6e8f0a1
Create Date: 2026-08-16 00:00:00.000000

Wave 1 P0-2. The original `ck_addon_catalog_type` CHECK (shipped in
f4a8c2b91d70) omitted 'PROJECT_SERVICE_COVERAGE', while models.ADDON_TYPES and
app.ADDON_PURCHASABLE_TYPES both include it and the entire standalone
project-renewal product is built on it. On any migrated (i.e. production)
database the row could not be inserted at all. The historical revision is NOT
edited - this is a new revision that replaces the constraint in place.

Existing rows are preserved: the new predicate is a strict superset of the old
one, so every row that satisfied the old CHECK satisfies the new one, and
genuinely invalid types are still rejected.
"""
from alembic import op
import sqlalchemy as sa


revision = "c3f7a1d5e9b4"
down_revision = "b2c4d6e8f0a1"
branch_labels = None
depends_on = None


CONSTRAINT_NAME = "ck_addon_catalog_type"
OLD_TYPES = ("EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY")
NEW_TYPES = ("EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE")


def _predicate(types):
    return "addon_type IN (" + ", ".join("'%s'" % value for value in types) + ")"


def _replace_check(allowed):
    # batch_alter_table so SQLite (which cannot ALTER a CHECK) is handled by
    # table recreation, exactly as every other constraint change in this chain.
    with op.batch_alter_table("addon_catalog") as batch_op:
        try:
            batch_op.drop_constraint(CONSTRAINT_NAME, type_="check")
        except Exception:
            # A database created by db.create_all() before the model carried a
            # matching __table_args__ constraint has no constraint to drop.
            # Creating the correct one below is still the desired end state.
            pass
        batch_op.create_check_constraint(CONSTRAINT_NAME, _predicate(allowed))


def upgrade():
    _replace_check(NEW_TYPES)


def downgrade():
    # Only safe while no row uses the newly permitted type; refuse rather than
    # silently destroy commercial data.
    bind = op.get_bind()
    offending = bind.execute(
        sa.text("SELECT COUNT(*) FROM addon_catalog WHERE addon_type = 'PROJECT_SERVICE_COVERAGE'")
    ).scalar()
    if offending:
        raise RuntimeError(
            "Cannot downgrade %s: %d addon_catalog row(s) use PROJECT_SERVICE_COVERAGE. "
            "Deactivate and remove them first." % (revision, offending)
        )
    _replace_check(OLD_TYPES)
