"""project targeted entitlements and content reports

Revision ID: a1c3e5b7d9f2
Revises: e5f6a7b8c9d0
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "a1c3e5b7d9f2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable by design: EXTRA_SCANS / VALIDITY_EXTENSION / PROJECT_CAPACITY
    # stay account-level and carry no project. Only project-targeted types
    # (PROJECT_SERVICE_COVERAGE) populate this, enforced in the service layer.
    with op.batch_alter_table("addon_purchases") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_addon_purchases_project_id", "projects", ["project_id"], ["id"]
        )
        batch_op.create_index("ix_addon_purchases_project_id", ["project_id"])

    with op.batch_alter_table("entitlement_transactions") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_entitlement_transactions_project_id", "projects", ["project_id"], ["id"]
        )
        batch_op.create_index("ix_entitlement_transactions_project_id", ["project_id"])

    op.create_table(
        "content_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("reporter_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reporter_email", sa.String(length=255), nullable=True),
        sa.Column("reporter_session_hash", sa.String(length=64), nullable=True),
        sa.Column("reporter_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("resolution_action", sa.String(length=40), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_content_reports_project_id", "content_reports", ["project_id"])
    op.create_index("ix_content_reports_reporter_user_id", "content_reports", ["reporter_user_id"])
    op.create_index("ix_content_reports_reporter_session_hash", "content_reports", ["reporter_session_hash"])
    op.create_index("ix_content_reports_reporter_ip_hash", "content_reports", ["reporter_ip_hash"])
    op.create_index("ix_content_reports_reason", "content_reports", ["reason"])
    op.create_index("ix_content_reports_status", "content_reports", ["status"])
    op.create_index("ix_content_reports_reviewed_by_admin_id", "content_reports", ["reviewed_by_admin_id"])
    op.create_index("ix_content_reports_project_status", "content_reports", ["project_id", "status"])
    op.create_index("ix_content_reports_created_at", "content_reports", ["created_at"])


def downgrade():
    op.drop_index("ix_content_reports_created_at", table_name="content_reports")
    op.drop_index("ix_content_reports_project_status", table_name="content_reports")
    op.drop_index("ix_content_reports_reviewed_by_admin_id", table_name="content_reports")
    op.drop_index("ix_content_reports_status", table_name="content_reports")
    op.drop_index("ix_content_reports_reason", table_name="content_reports")
    op.drop_index("ix_content_reports_reporter_ip_hash", table_name="content_reports")
    op.drop_index("ix_content_reports_reporter_session_hash", table_name="content_reports")
    op.drop_index("ix_content_reports_reporter_user_id", table_name="content_reports")
    op.drop_index("ix_content_reports_project_id", table_name="content_reports")
    op.drop_table("content_reports")

    with op.batch_alter_table("entitlement_transactions") as batch_op:
        batch_op.drop_index("ix_entitlement_transactions_project_id")
        batch_op.drop_constraint("fk_entitlement_transactions_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")

    with op.batch_alter_table("addon_purchases") as batch_op:
        batch_op.drop_index("ix_addon_purchases_project_id")
        batch_op.drop_constraint("fk_addon_purchases_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")
