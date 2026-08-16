"""ownership claim audit metadata (V1.1 Wave 4)

Revision ID: a9d3c7e1b502
Revises: f2b7d4e9c3a6
Create Date: 2026-08-16 23:30:00.000000

ONE COLUMN, ONE REASON.
project_ownership_transfers already carries `metadata_json`, which Wave 4 uses
as the append-only governed-transition trail (who acted, previous/new state,
and the capacity numbers that were actually checked when a transfer parked in
PENDING_CAPACITY). project_ownership_claims had no equivalent, so a vendor
response note and the claim's own transition history had nowhere to live
without overloading `decision_reason` (the Admin's field) or `evidence_json`
(the claimant's field). Keeping those three separate is the whole point.

Nullable TEXT with no default: existing claim rows read as "no recorded trail",
which is honest, and no backfill is invented. Additive only - nothing is
renamed, retyped or dropped, so downgrade is a clean column drop.
"""
from alembic import op
import sqlalchemy as sa


revision = "a9d3c7e1b502"
down_revision = "f2b7d4e9c3a6"
branch_labels = None
depends_on = None


TABLE = "project_ownership_claims"
COLUMN = "metadata_json"


def _has_column():
    return COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade():
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade():
    if not _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
