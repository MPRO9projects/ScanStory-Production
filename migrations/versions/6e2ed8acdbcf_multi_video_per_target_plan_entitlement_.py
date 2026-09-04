"""multi video per target plan entitlement foundation (Issue 3E-A)

Revision ID: 6e2ed8acdbcf
Revises: a4f2c8d9e1b7
Create Date: 2026-08-22 13:56:30.708416

Adds the commercial ENTITLEMENT foundation for the future one-target/many-
videos capability (PairMedia, add-video, scanner media chooser). None of
that is built yet - this migration only gives the future feature a proper
server-side plan contract:

  subscription_plans
    allow_multi_video_per_target   plan may enable multiple videos per target
    max_videos_per_target          plan-configured cap, still bounded by the
                                    immutable MAX_VIDEOS_PER_TARGET_CEILING
                                    in entitlements.py

BACKFILL POLICY - deliberately the OPPOSITE of Wave 2's rule.

Wave 2's experience flags (allow_direct_qr etc.) defaulted TRUE because they
codified behaviour every plan ALREADY had unrestricted - defaulting FALSE
there would have retroactively revoked a capability live accounts already
used.

This is the reverse case: multi-video-per-target is a BRAND NEW capability
that does not exist today at all (every target has exactly one video,
unconditionally, with no column anywhere expressing otherwise). Defaulting
this to TRUE would silently hand every existing plan - including free/trial
plans - a new sellable feature for free the instant this migration runs.
So allow_multi_video_per_target defaults FALSE for every existing row, and
max_videos_per_target defaults NULL (no plan-specific cap configured,
consistent with "unset" meaning the same as it does for the other nullable
per-file media policy columns Wave 2 added). A superadmin must explicitly
enable and configure this per plan via /admin/plans/<id>/edit.

Reversible: downgrade drops both columns. No data loss on downgrade because
nothing outside these two columns reads or writes them yet (PairMedia does
not exist) - dropping them cannot orphan any other row.

PostgreSQL/SQLite compatible: server_default supplied for the NOT NULL
column so existing rows backfill deterministically in a single ALTER;
wrapped in batch_alter_table for SQLite's table-rebuild semantics, matching
every other plan-column migration in this chain.
"""
from alembic import op
import sqlalchemy as sa


revision = "6e2ed8acdbcf"
down_revision = "a4f2c8d9e1b7"
branch_labels = None
depends_on = None


PLAN_COLUMNS = (
    # (name, type, nullable, server_default)
    ("allow_multi_video_per_target", sa.Boolean(), False, sa.false()),
    ("max_videos_per_target", sa.Integer(), True, None),
)


def _existing_columns(table):
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def _add(batch, table, columns):
    present = _existing_columns(table)
    for name, type_, nullable, default in columns:
        if name in present:
            continue
        batch.add_column(
            sa.Column(name, type_, nullable=nullable, server_default=default)
        )


def upgrade():
    with op.batch_alter_table("subscription_plans") as batch:
        _add(batch, "subscription_plans", PLAN_COLUMNS)


def downgrade():
    with op.batch_alter_table("subscription_plans") as batch:
        for name, *_ in PLAN_COLUMNS:
            batch.drop_column(name)
