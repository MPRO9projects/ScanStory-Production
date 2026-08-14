"""addon entitlement foundation

Revision ID: f4a8c2b91d70
Revises: d6b9c1f4a2e8
Create Date: 2026-08-14 23:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "f4a8c2b91d70"
down_revision = "d6b9c1f4a2e8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "addon_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("addon_type", sa.String(length=40), nullable=False),
        sa.Column("unit_amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("scan_delta", sa.Integer(), nullable=True),
        sa.Column("validity_days_delta", sa.Integer(), nullable=True),
        sa.Column("project_delta", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_commercially_available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("addon_type IN ('EXTRA_SCANS', 'VALIDITY_EXTENSION', 'PROJECT_CAPACITY')", name="ck_addon_catalog_type"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_addon_catalog_code", "addon_catalog", ["code"])
    op.create_index("ix_addon_catalog_addon_type", "addon_catalog", ["addon_type"])

    op.create_table(
        "addon_purchases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("catalog_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("total_amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("razorpay_order_id", sa.String(length=255), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(length=255), nullable=True),
        sa.Column("razorpay_signature", sa.String(length=512), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'fulfilled', 'failed', 'cancelled', 'refunded')", name="ck_addon_purchases_status"),
        sa.ForeignKeyConstraint(["catalog_id"], ["addon_catalog.id"], ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
        sa.UniqueConstraint("razorpay_order_id"),
        sa.UniqueConstraint("razorpay_payment_id"),
    )
    op.create_index("ix_addon_purchases_order_id", "addon_purchases", ["order_id"])
    op.create_index("ix_addon_purchases_user_id", "addon_purchases", ["user_id"])
    op.create_index("ix_addon_purchases_catalog_id", "addon_purchases", ["catalog_id"])
    op.create_index("ix_addon_purchases_status", "addon_purchases", ["status"])
    op.create_index("ix_addon_purchases_razorpay_order_id", "addon_purchases", ["razorpay_order_id"])
    op.create_index("ix_addon_purchases_razorpay_payment_id", "addon_purchases", ["razorpay_payment_id"])

    op.create_table(
        "entitlement_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("entitlement_type", sa.String(length=40), nullable=False),
        sa.Column("delta_value", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("entitlement_type IN ('EXTRA_SCANS', 'VALIDITY_EXTENSION', 'PROJECT_CAPACITY')", name="ck_entitlement_transactions_type"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", "entitlement_type", name="uq_entitlement_source_type_id_type"),
    )
    op.create_index("ix_entitlement_transactions_user_id", "entitlement_transactions", ["user_id"])
    op.create_index("ix_entitlement_transactions_entitlement_type", "entitlement_transactions", ["entitlement_type"])
    op.create_index("ix_entitlement_transactions_source_type", "entitlement_transactions", ["source_type"])
    op.create_index("ix_entitlement_transactions_source_id", "entitlement_transactions", ["source_id"])

    with op.batch_alter_table("razorpay_webhook_events") as batch_op:
        batch_op.add_column(sa.Column("addon_purchase_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_webhook_events_addon_purchase_id", "addon_purchases", ["addon_purchase_id"], ["id"])
        batch_op.create_index("ix_razorpay_webhook_events_addon_purchase_id", ["addon_purchase_id"])


def downgrade():
    with op.batch_alter_table("razorpay_webhook_events") as batch_op:
        batch_op.drop_index("ix_razorpay_webhook_events_addon_purchase_id")
        batch_op.drop_constraint("fk_webhook_events_addon_purchase_id", type_="foreignkey")
        batch_op.drop_column("addon_purchase_id")

    op.drop_index("ix_entitlement_transactions_source_id", table_name="entitlement_transactions")
    op.drop_index("ix_entitlement_transactions_source_type", table_name="entitlement_transactions")
    op.drop_index("ix_entitlement_transactions_entitlement_type", table_name="entitlement_transactions")
    op.drop_index("ix_entitlement_transactions_user_id", table_name="entitlement_transactions")
    op.drop_table("entitlement_transactions")

    op.drop_index("ix_addon_purchases_razorpay_payment_id", table_name="addon_purchases")
    op.drop_index("ix_addon_purchases_razorpay_order_id", table_name="addon_purchases")
    op.drop_index("ix_addon_purchases_status", table_name="addon_purchases")
    op.drop_index("ix_addon_purchases_catalog_id", table_name="addon_purchases")
    op.drop_index("ix_addon_purchases_user_id", table_name="addon_purchases")
    op.drop_index("ix_addon_purchases_order_id", table_name="addon_purchases")
    op.drop_table("addon_purchases")

    op.drop_index("ix_addon_catalog_addon_type", table_name="addon_catalog")
    op.drop_index("ix_addon_catalog_code", table_name="addon_catalog")
    op.drop_table("addon_catalog")
