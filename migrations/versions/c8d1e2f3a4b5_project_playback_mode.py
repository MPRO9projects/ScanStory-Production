"""project playback mode

Revision ID: c8d1e2f3a4b5
Revises: b7c9d2e4f6a1
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "c8d1e2f3a4b5"
down_revision = "b7c9d2e4f6a1"
branch_labels = None
depends_on = None


PLAYBACK_MODES = ("tracked_overlay", "detect_once", "direct")


def upgrade():
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "playback_mode",
                sa.String(length=30),
                nullable=False,
                server_default="tracked_overlay",
            )
        )
        batch_op.create_check_constraint(
            "ck_projects_playback_mode",
            f"playback_mode IN {PLAYBACK_MODES}",
        )
    op.execute(
        "UPDATE projects SET playback_mode = CASE "
        "WHEN experience_type = 'direct_qr' THEN 'direct' "
        "ELSE 'tracked_overlay' END"
    )


def downgrade():
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("ck_projects_playback_mode", type_="check")
        batch_op.drop_column("playback_mode")
