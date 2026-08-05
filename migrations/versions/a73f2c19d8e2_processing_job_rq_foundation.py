"""processing job rq foundation

Revision ID: a73f2c19d8e2
Revises: 54a108a17fa7
Create Date: 2026-08-05 23:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "a73f2c19d8e2"
down_revision = "ebeab1cf4ec9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pair_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("owner_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("owner_admin_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("queue_job_id", sa.String(length=191), nullable=True))
        batch_op.add_column(sa.Column("queued_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("failed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("safe_error_code", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("safe_error_summary", sa.String(length=500), nullable=True))
        batch_op.create_foreign_key("fk_processing_jobs_project_id_projects", "projects", ["project_id"], ["id"])
        batch_op.create_foreign_key("fk_processing_jobs_pair_id_project_pairs", "project_pairs", ["pair_id"], ["id"])
        batch_op.create_foreign_key("fk_processing_jobs_owner_user_id_users", "users", ["owner_user_id"], ["id"])
        batch_op.create_foreign_key("fk_processing_jobs_owner_admin_id_admins", "admins", ["owner_admin_id"], ["id"])
        batch_op.create_unique_constraint(
            "uq_processing_job_project_idempotency",
            ["project_id", "idempotency_key"],
        )

    op.create_index("ix_processing_jobs_project_id", "processing_jobs", ["project_id"])
    op.create_index("ix_processing_jobs_pair_id", "processing_jobs", ["pair_id"])
    op.create_index("ix_processing_jobs_owner_user_id", "processing_jobs", ["owner_user_id"])
    op.create_index("ix_processing_jobs_owner_admin_id", "processing_jobs", ["owner_admin_id"])
    op.create_index("ix_processing_jobs_queue_job_id", "processing_jobs", ["queue_job_id"])
    op.create_index("ix_processing_jobs_project_status", "processing_jobs", ["project_id", "status"])
    op.create_index("ix_processing_jobs_pair_status", "processing_jobs", ["pair_id", "status"])
    op.create_index("ix_processing_jobs_type_status", "processing_jobs", ["job_type", "status"])
    op.create_index("ix_processing_jobs_owner_user_status", "processing_jobs", ["owner_user_id", "status"])
    op.create_index("ix_processing_jobs_owner_admin_status", "processing_jobs", ["owner_admin_id", "status"])


def downgrade():
    op.drop_index("ix_processing_jobs_owner_admin_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_owner_user_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_type_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_pair_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_project_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_queue_job_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_owner_admin_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_owner_user_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_pair_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_project_id", table_name="processing_jobs")

    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.drop_constraint("uq_processing_job_project_idempotency", type_="unique")
        batch_op.drop_constraint("fk_processing_jobs_owner_admin_id_admins", type_="foreignkey")
        batch_op.drop_constraint("fk_processing_jobs_owner_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_processing_jobs_pair_id_project_pairs", type_="foreignkey")
        batch_op.drop_constraint("fk_processing_jobs_project_id_projects", type_="foreignkey")
        batch_op.drop_column("safe_error_summary")
        batch_op.drop_column("safe_error_code")
        batch_op.drop_column("last_heartbeat_at")
        batch_op.drop_column("failed_at")
        batch_op.drop_column("queued_at")
        batch_op.drop_column("queue_job_id")
        batch_op.drop_column("owner_admin_id")
        batch_op.drop_column("owner_user_id")
        batch_op.drop_column("pair_id")
        batch_op.drop_column("project_id")
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
