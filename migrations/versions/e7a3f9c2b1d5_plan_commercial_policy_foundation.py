"""plan commercial policy foundation (V1.1 Wave 2)

Revision ID: e7a3f9c2b1d5
Revises: d4e8b2c6a0f3
Create Date: 2026-08-16 17:30:00.000000

Adds the commercial ENTITLEMENT foundation that later storage accounting, plan
UX, upgrade/downgrade and add-on work will consume:

  subscription_plans
    plan_family                 INDIVIDUAL | BUSINESS_VENDOR
    lifecycle_status            DRAFT | ACTIVE | CLOSED_FOR_NEW_PURCHASE | ARCHIVED
    plan_revision               version marker for commercial policy edits
    max_image_bytes             per-file media policy (NULL = plan adds no cap)
    max_video_bytes
    max_video_duration_seconds
    max_image_dimension_px
    max_image_pixels
    base_storage_bytes          ENTITLEMENT allowance only (Wave 3 does usage)

    allow_direct_qr / allow_detect_once / allow_tracked_overlay

  users
    pending_plan_id             deferred (downgrade) plan change
    pending_plan_effective_at

  payment_orders
    plan_policy_snapshot_json   what the subscriber actually agreed to
    is_deferred_plan_change

BACKFILL POLICY - no invented commercial values.
Every default below was chosen because it preserves the EXACT current behaviour
of every existing plan row, not because it is a real commercial number:

  * plan_family        -> INDIVIDUAL. There is no pre-existing per-plan signal
    to infer a family from (no plan column has ever referenced account_type),
    and INDIVIDUAL is the only product that has existed, so it is the safe
    inference rather than a guess.
  * lifecycle_status   -> ACTIVE, so no currently-sellable plan stops selling.
  * plan_revision      -> 1.
  * media policy       -> NULL, meaning "this plan imposes no cap of its own".
    Enforcement is min(plan, immutable server ceiling), so NULL leaves the
    server ceiling as the only effective limit - byte-for-byte today's rule.
  * base_storage_bytes -> NULL ("unspecified"). No storage entitlement has ever
    been sold, and inventing a quota here would create one.
  * experience flags   -> TRUE, because every plan can currently create every
    experience/playback combination. Defaulting FALSE would retroactively
    revoke capability from live accounts.

BigInteger is used for every byte-valued column. Integer caps at ~2.1GB, which
the Wave 1 audit flagged as a real risk for byte counts (MAX_VIDEO_SIZE alone
defaults to 1GB and a storage allowance is far larger).

PostgreSQL/SQLite compatible: server_default is supplied for every NOT NULL
column so existing rows backfill deterministically in a single ALTER, and the
adds are wrapped in batch_alter_table for SQLite's table-rebuild semantics.
"""
from alembic import op
import sqlalchemy as sa


revision = "e7a3f9c2b1d5"
down_revision = "d4e8b2c6a0f3"
branch_labels = None
depends_on = None


PLAN_COLUMNS = (
    # (name, type, nullable, server_default)
    ("plan_family", sa.String(length=30), False, "INDIVIDUAL"),
    ("lifecycle_status", sa.String(length=40), False, "ACTIVE"),
    ("plan_revision", sa.Integer(), False, "1"),
    ("max_image_bytes", sa.BigInteger(), True, None),
    ("max_video_bytes", sa.BigInteger(), True, None),
    ("max_video_duration_seconds", sa.Integer(), True, None),
    ("max_image_dimension_px", sa.Integer(), True, None),
    ("max_image_pixels", sa.BigInteger(), True, None),
    ("base_storage_bytes", sa.BigInteger(), True, None),
    ("allow_direct_qr", sa.Boolean(), False, sa.true()),
    ("allow_detect_once", sa.Boolean(), False, sa.true()),
    ("allow_tracked_overlay", sa.Boolean(), False, sa.true()),
)

USER_COLUMNS = (
    ("pending_plan_id", sa.Integer(), True, None),
    ("pending_plan_effective_at", sa.DateTime(), True, None),
)

ORDER_COLUMNS = (
    ("plan_policy_snapshot_json", sa.Text(), True, None),
    ("is_deferred_plan_change", sa.Boolean(), False, sa.false()),
)

PLAN_INDEXES = (
    ("ix_subscription_plans_plan_family", "plan_family"),
    ("ix_subscription_plans_lifecycle_status", "lifecycle_status"),
)


def _existing_columns(table):
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def _existing_indexes(table):
    inspector = sa.inspect(op.get_bind())
    return {i["name"] for i in inspector.get_indexes(table)}


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

    existing_idx = _existing_indexes("subscription_plans")
    for index_name, column in PLAN_INDEXES:
        if index_name not in existing_idx:
            op.create_index(index_name, "subscription_plans", [column])

    with op.batch_alter_table("users") as batch:
        _add(batch, "users", USER_COLUMNS)
        # Named explicitly so the downgrade can drop it on PostgreSQL, where an
        # unnamed FK gets a server-assigned name.
        batch.create_foreign_key(
            "fk_users_pending_plan_id_subscription_plans",
            "subscription_plans",
            ["pending_plan_id"],
            ["id"],
        )

    with op.batch_alter_table("payment_orders") as batch:
        _add(batch, "payment_orders", ORDER_COLUMNS)


def downgrade():
    with op.batch_alter_table("payment_orders") as batch:
        for name, *_ in ORDER_COLUMNS:
            batch.drop_column(name)

    with op.batch_alter_table("users") as batch:
        batch.drop_constraint(
            "fk_users_pending_plan_id_subscription_plans", type_="foreignkey"
        )
        for name, *_ in USER_COLUMNS:
            batch.drop_column(name)

    for index_name, _column in PLAN_INDEXES:
        op.drop_index(index_name, table_name="subscription_plans")

    with op.batch_alter_table("subscription_plans") as batch:
        for name, *_ in PLAN_COLUMNS:
            batch.drop_column(name)
