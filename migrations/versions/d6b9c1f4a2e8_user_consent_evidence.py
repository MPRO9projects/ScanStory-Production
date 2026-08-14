"""user consent evidence

Revision ID: d6b9c1f4a2e8
Revises: 0b8fffb4c614
Create Date: 2026-08-14 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "d6b9c1f4a2e8"
down_revision = "0b8fffb4c614"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_consent_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("consent_type", sa.String(length=30), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("source_context", sa.String(length=80), nullable=False),
        sa.Column("evidence_metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("consent_type IN ('TERMS', 'PRIVACY')", name="ck_user_consent_evidence_type"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "consent_type",
            "policy_version",
            "source_context",
            name="uq_user_consent_type_version_source",
        ),
    )
    op.create_index("ix_user_consent_evidence_user_id", "user_consent_evidence", ["user_id"])
    op.create_index("ix_user_consent_evidence_consent_type", "user_consent_evidence", ["consent_type"])
    op.create_index("ix_user_consent_evidence_accepted_at", "user_consent_evidence", ["accepted_at"])


def downgrade():
    op.drop_index("ix_user_consent_evidence_accepted_at", table_name="user_consent_evidence")
    op.drop_index("ix_user_consent_evidence_consent_type", table_name="user_consent_evidence")
    op.drop_index("ix_user_consent_evidence_user_id", table_name="user_consent_evidence")
    op.drop_table("user_consent_evidence")
