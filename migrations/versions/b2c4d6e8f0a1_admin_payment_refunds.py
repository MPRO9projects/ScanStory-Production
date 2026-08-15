"""admin payment refunds

Revision ID: b2c4d6e8f0a1
Revises: a1c3e5b7d9f2
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "b2c4d6e8f0a1"
down_revision = "a1c3e5b7d9f2"
branch_labels = None
depends_on = None


REFUND_STATUSES = ("REFUND_REQUESTED", "REFUND_PROCESSING", "REFUNDED", "REFUND_FAILED")
RECONCILIATION_STATUSES = ("PENDING", "APPLIED", "MANUAL_REVIEW_REQUIRED", "FAILED")


def upgrade():
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_order_id", sa.Integer(), sa.ForeignKey("payment_orders.id"), nullable=True),
        sa.Column("addon_purchase_id", sa.Integer(), sa.ForeignKey("addon_purchases.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("provider", sa.String(length=30), nullable=False, server_default="RAZORPAY"),
        sa.Column("provider_refund_id", sa.String(length=255), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False),
        sa.Column("provider_status", sa.String(length=40), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="REFUND_REQUESTED"),
        sa.Column("reconciliation_status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("reconciliation_message_safe", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_message_safe", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(payment_order_id IS NOT NULL AND addon_purchase_id IS NULL) OR "
            "(payment_order_id IS NULL AND addon_purchase_id IS NOT NULL)",
            name="ck_payment_refunds_exactly_one_source",
        ),
        sa.CheckConstraint("provider = 'RAZORPAY'", name="ck_payment_refunds_provider"),
        sa.CheckConstraint(f"status IN {REFUND_STATUSES}", name="ck_payment_refunds_status"),
        sa.CheckConstraint(
            f"reconciliation_status IN {RECONCILIATION_STATUSES}",
            name="ck_payment_refunds_reconciliation_status",
        ),
        sa.UniqueConstraint("payment_order_id", name="uq_payment_refunds_payment_order_id"),
        sa.UniqueConstraint("addon_purchase_id", name="uq_payment_refunds_addon_purchase_id"),
        sa.UniqueConstraint("provider_refund_id", name="uq_payment_refunds_provider_refund_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_refunds_idempotency_key"),
    )
    op.create_index("ix_payment_refunds_payment_order_id", "payment_refunds", ["payment_order_id"])
    op.create_index("ix_payment_refunds_addon_purchase_id", "payment_refunds", ["addon_purchase_id"])
    op.create_index("ix_payment_refunds_user_id", "payment_refunds", ["user_id"])
    op.create_index("ix_payment_refunds_project_id", "payment_refunds", ["project_id"])
    op.create_index("ix_payment_refunds_provider_refund_id", "payment_refunds", ["provider_refund_id"])
    op.create_index("ix_payment_refunds_provider_payment_id", "payment_refunds", ["provider_payment_id"])
    op.create_index("ix_payment_refunds_status", "payment_refunds", ["status"])
    op.create_index("ix_payment_refunds_reconciliation_status", "payment_refunds", ["reconciliation_status"])
    op.create_index("ix_payment_refunds_requested_by_admin_id", "payment_refunds", ["requested_by_admin_id"])

    with op.batch_alter_table("razorpay_webhook_events") as batch_op:
        batch_op.add_column(sa.Column("payment_refund_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_webhook_events_payment_refund_id", "payment_refunds", ["payment_refund_id"], ["id"])
        batch_op.create_index("ix_razorpay_webhook_events_payment_refund_id", ["payment_refund_id"])

    with op.batch_alter_table("project_service_coverages") as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("revoked_by_refund_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_project_service_coverages_revoked_by_refund_id", "payment_refunds", ["revoked_by_refund_id"], ["id"])
        batch_op.create_index("ix_project_service_coverages_revoked_by_refund_id", ["revoked_by_refund_id"])


def downgrade():
    with op.batch_alter_table("project_service_coverages") as batch_op:
        batch_op.drop_index("ix_project_service_coverages_revoked_by_refund_id")
        batch_op.drop_constraint("fk_project_service_coverages_revoked_by_refund_id", type_="foreignkey")
        batch_op.drop_column("revoked_by_refund_id")
        batch_op.drop_column("revoked_at")

    with op.batch_alter_table("razorpay_webhook_events") as batch_op:
        batch_op.drop_index("ix_razorpay_webhook_events_payment_refund_id")
        batch_op.drop_constraint("fk_webhook_events_payment_refund_id", type_="foreignkey")
        batch_op.drop_column("payment_refund_id")

    op.drop_index("ix_payment_refunds_requested_by_admin_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_reconciliation_status", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_status", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_provider_payment_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_provider_refund_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_project_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_user_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_addon_purchase_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_payment_order_id", table_name="payment_refunds")
    op.drop_table("payment_refunds")
