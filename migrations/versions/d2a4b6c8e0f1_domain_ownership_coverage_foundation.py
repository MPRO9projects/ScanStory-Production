"""domain ownership and service coverage foundation

Revision ID: d2a4b6c8e0f1
Revises: c8d1e2f3a4b5
Create Date: 2026-08-15 00:00:00.000000
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "d2a4b6c8e0f1"
down_revision = "c8d1e2f3a4b5"
branch_labels = None
depends_on = None


ACCOUNT_TYPES = ("INDIVIDUAL", "BUSINESS_VENDOR")
TRANSFER_STATUSES = ("PENDING_ACCEPTANCE", "PENDING_CAPACITY", "COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED")
CLAIM_STATUSES = (
    "OPEN",
    "VENDOR_NOTIFIED",
    "APPROVED_BY_VENDOR",
    "PENDING_ADMIN_REVIEW",
    "APPROVED_BY_ADMIN",
    "REJECTED",
    "CANCELLED",
    "EXPIRED",
    "TRANSFER_COMPLETED",
)
COVERAGE_SOURCE_TYPES = (
    "OWNER_SUBSCRIPTION",
    "STANDALONE_PROJECT_RENEWAL",
    "TRANSFER_CARRY_OVER",
    "ADMIN_GRANT",
    "LEGACY_COMPATIBILITY",
)
COVERAGE_STATUSES = ("ACTIVE", "REVOKED", "EXPIRED", "SUPERSEDED")


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("account_type", sa.String(length=30), nullable=False, server_default="INDIVIDUAL"))
        batch_op.create_check_constraint("ck_users_account_type", f"account_type IN {ACCOUNT_TYPES}")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("created_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("current_owner_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("manager_vendor_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("beneficiary_user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_projects_created_by_user_id", "users", ["created_by_user_id"], ["id"])
        batch_op.create_foreign_key("fk_projects_current_owner_user_id", "users", ["current_owner_user_id"], ["id"])
        batch_op.create_foreign_key("fk_projects_manager_vendor_user_id", "users", ["manager_vendor_user_id"], ["id"])
        batch_op.create_foreign_key("fk_projects_beneficiary_user_id", "users", ["beneficiary_user_id"], ["id"])
        batch_op.create_index("ix_projects_created_by_user_id", ["created_by_user_id"])
        batch_op.create_index("ix_projects_current_owner_user_id", ["current_owner_user_id"])
        batch_op.create_index("ix_projects_manager_vendor_user_id", ["manager_vendor_user_id"])
        batch_op.create_index("ix_projects_beneficiary_user_id", ["beneficiary_user_id"])

    op.create_table(
        "project_ownership_transfers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("initiated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("from_owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("retain_vendor_management", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING_ACCEPTANCE"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.CheckConstraint(f"status IN {TRANSFER_STATUSES}", name="ck_project_ownership_transfers_status"),
    )
    op.create_index("ix_project_ownership_transfers_project_id", "project_ownership_transfers", ["project_id"])
    op.create_index("ix_project_ownership_transfers_initiated_by_user_id", "project_ownership_transfers", ["initiated_by_user_id"])
    op.create_index("ix_project_ownership_transfers_from_owner_user_id", "project_ownership_transfers", ["from_owner_user_id"])
    op.create_index("ix_project_ownership_transfers_to_user_id", "project_ownership_transfers", ["to_user_id"])
    op.create_index("ix_project_ownership_transfers_completed_by_admin_id", "project_ownership_transfers", ["completed_by_admin_id"])
    op.create_index("ix_project_ownership_transfers_project_status", "project_ownership_transfers", ["project_id", "status"])
    op.create_index("ix_project_ownership_transfers_to_status", "project_ownership_transfers", ["to_user_id", "status"])
    op.create_index("ix_project_ownership_transfers_from_status", "project_ownership_transfers", ["from_owner_user_id", "status"])

    op.create_table(
        "project_ownership_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("claimant_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("current_owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("vendor_notified_at", sa.DateTime(), nullable=True),
        sa.Column("response_deadline_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("transfer_id", sa.Integer(), sa.ForeignKey("project_ownership_transfers.id"), nullable=True),
        sa.CheckConstraint(f"status IN {CLAIM_STATUSES}", name="ck_project_ownership_claims_status"),
    )
    op.create_index("ix_project_ownership_claims_project_id", "project_ownership_claims", ["project_id"])
    op.create_index("ix_project_ownership_claims_claimant_user_id", "project_ownership_claims", ["claimant_user_id"])
    op.create_index("ix_project_ownership_claims_current_owner_user_id", "project_ownership_claims", ["current_owner_user_id"])
    op.create_index("ix_project_ownership_claims_reviewed_by_admin_id", "project_ownership_claims", ["reviewed_by_admin_id"])
    op.create_index("ix_project_ownership_claims_transfer_id", "project_ownership_claims", ["transfer_id"])
    op.create_index("ix_project_ownership_claims_project_status", "project_ownership_claims", ["project_id", "status"])
    op.create_index("ix_project_ownership_claims_claimant_status", "project_ownership_claims", ["claimant_user_id", "status"])

    op.create_table(
        "project_service_coverages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("source_reference", sa.String(length=120), nullable=True),
        sa.Column("coverage_start", sa.DateTime(), nullable=False),
        sa.Column("coverage_end", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(f"source_type IN {COVERAGE_SOURCE_TYPES}", name="ck_project_service_coverages_source_type"),
        sa.CheckConstraint(f"status IN {COVERAGE_STATUSES}", name="ck_project_service_coverages_status"),
    )
    op.create_index("ix_project_service_coverages_project_id", "project_service_coverages", ["project_id"])
    op.create_index("ix_project_service_coverages_created_by_user_id", "project_service_coverages", ["created_by_user_id"])
    op.create_index("ix_project_service_coverages_created_by_admin_id", "project_service_coverages", ["created_by_admin_id"])
    op.create_index("ix_project_service_coverages_project_status_end", "project_service_coverages", ["project_id", "status", "coverage_end"])
    op.create_index("ix_project_service_coverages_source", "project_service_coverages", ["source_type", "source_id", "source_reference"])

    projects = sa.table(
        "projects",
        sa.column("id", sa.Integer()),
        sa.column("owner_user_id", sa.Integer()),
        sa.column("created_by_user_id", sa.Integer()),
        sa.column("current_owner_user_id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
        sa.column("is_active", sa.Boolean()),
    )
    op.execute(
        projects.update()
        .where(projects.c.owner_user_id.isnot(None))
        .values(
            created_by_user_id=projects.c.owner_user_id,
            current_owner_user_id=projects.c.owner_user_id,
        )
    )

    coverages = sa.table(
        "project_service_coverages",
        sa.column("project_id", sa.Integer()),
        sa.column("source_type", sa.String()),
        sa.column("source_reference", sa.String()),
        sa.column("coverage_start", sa.DateTime()),
        sa.column("coverage_end", sa.DateTime()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("reason", sa.Text()),
    )
    conn = op.get_bind()
    active_rows = conn.execute(
        sa.select(projects.c.id, projects.c.created_at).where(projects.c.is_active == sa.true())
    ).fetchall()
    now = datetime.utcnow()
    if active_rows:
        op.bulk_insert(
            coverages,
            [
                {
                    "project_id": row.id,
                    "source_type": "LEGACY_COMPATIBILITY",
                    "source_reference": revision,
                    "coverage_start": row.created_at or now,
                    "coverage_end": None,
                    "status": "ACTIVE",
                    "created_at": now,
                    "reason": "Preserve existing public QR availability until coverage is normalized.",
                }
                for row in active_rows
            ],
        )


def downgrade():
    op.drop_index("ix_project_service_coverages_source", table_name="project_service_coverages")
    op.drop_index("ix_project_service_coverages_project_status_end", table_name="project_service_coverages")
    op.drop_index("ix_project_service_coverages_created_by_admin_id", table_name="project_service_coverages")
    op.drop_index("ix_project_service_coverages_created_by_user_id", table_name="project_service_coverages")
    op.drop_index("ix_project_service_coverages_project_id", table_name="project_service_coverages")
    op.drop_table("project_service_coverages")

    op.drop_index("ix_project_ownership_claims_claimant_status", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_project_status", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_transfer_id", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_reviewed_by_admin_id", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_current_owner_user_id", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_claimant_user_id", table_name="project_ownership_claims")
    op.drop_index("ix_project_ownership_claims_project_id", table_name="project_ownership_claims")
    op.drop_table("project_ownership_claims")

    op.drop_index("ix_project_ownership_transfers_from_status", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_to_status", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_project_status", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_completed_by_admin_id", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_to_user_id", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_from_owner_user_id", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_initiated_by_user_id", table_name="project_ownership_transfers")
    op.drop_index("ix_project_ownership_transfers_project_id", table_name="project_ownership_transfers")
    op.drop_table("project_ownership_transfers")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_beneficiary_user_id")
        batch_op.drop_index("ix_projects_manager_vendor_user_id")
        batch_op.drop_index("ix_projects_current_owner_user_id")
        batch_op.drop_index("ix_projects_created_by_user_id")
        batch_op.drop_constraint("fk_projects_beneficiary_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_projects_manager_vendor_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_projects_current_owner_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_projects_created_by_user_id", type_="foreignkey")
        batch_op.drop_column("beneficiary_user_id")
        batch_op.drop_column("manager_vendor_user_id")
        batch_op.drop_column("current_owner_user_id")
        batch_op.drop_column("created_by_user_id")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_account_type", type_="check")
        batch_op.drop_column("account_type")
