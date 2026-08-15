"""upload session experience contract

Revision ID: e5f6a7b8c9d0
Revises: d2a4b6c8e0f1
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d2a4b6c8e0f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.add_column(sa.Column("experience_type", sa.String(length=30), nullable=False, server_default="image_video"))
        batch_op.add_column(sa.Column("playback_mode", sa.String(length=30), nullable=False, server_default="tracked_overlay"))
        batch_op.create_check_constraint("ck_upload_sessions_experience_type", "experience_type IN ('image_video', 'direct_qr')")
        batch_op.create_check_constraint("ck_upload_sessions_playback_mode", "playback_mode IN ('tracked_overlay', 'detect_once', 'direct')")


def downgrade():
    with op.batch_alter_table("upload_sessions") as batch_op:
        batch_op.drop_constraint("ck_upload_sessions_playback_mode", type_="check")
        batch_op.drop_constraint("ck_upload_sessions_experience_type", type_="check")
        batch_op.drop_column("playback_mode")
        batch_op.drop_column("experience_type")
