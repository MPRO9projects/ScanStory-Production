"""project experience type

Revision ID: b7c9d2e4f6a1
Revises: f4a8c2b91d70
Create Date: 2026-08-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "b7c9d2e4f6a1"
down_revision = "f4a8c2b91d70"
branch_labels = None
depends_on = None


EXPERIENCE_TYPES = ("image_video", "direct_qr")


def upgrade():
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "experience_type",
                sa.String(length=30),
                nullable=False,
                server_default="image_video",
            )
        )
        batch_op.create_check_constraint(
            "ck_projects_experience_type",
            f"experience_type IN {EXPERIENCE_TYPES}",
        )
    op.execute("UPDATE projects SET experience_type = 'image_video' WHERE experience_type IS NULL")

    with op.batch_alter_table("project_pairs") as batch_op:
        batch_op.alter_column("image_filename", existing_type=sa.String(length=255), nullable=True)


def downgrade():
    # Direct QR rows have no marker image. Downgrading removes project-level
    # type knowledge, so use a harmless empty string to satisfy the old NOT
    # NULL schema without inventing a filesystem path.
    op.execute("UPDATE project_pairs SET image_filename = '' WHERE image_filename IS NULL")
    with op.batch_alter_table("project_pairs") as batch_op:
        batch_op.alter_column("image_filename", existing_type=sa.String(length=255), nullable=False)

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("ck_projects_experience_type", type_="check")
        batch_op.drop_column("experience_type")
