"""fast video phase 1 pair media derivative fields

Revision ID: 6f62644e4996
Revises: f53b3c212bba
Create Date: 2026-08-24 12:51:27.531864

Adds the optional Fast Video optimized-derivative fields to PairMedia
(video_filename - the original - is never touched, stays NOT NULL and
authoritative), plus a nullable pair_media_id FK on ProcessingJob so an
optimize_pair_media job can identify its target row by a real foreign key
rather than a filesystem path or an overload of the existing pair_id
(which continues to mean ProjectPair.id for every job type, this one
included).

No backfill: every existing PairMedia row simply gets
optimization_status='pending' and every other new column NULL, which is
already a fully valid, truthful state (no derivative has been attempted).

Reversible: downgrade drops exactly what upgrade added, in reverse order.
Neither pair_media.video_filename nor any ProjectPair column is touched by
either direction.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f62644e4996'
down_revision = 'f53b3c212bba'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pair_media") as batch_op:
        batch_op.add_column(sa.Column("optimized_video_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("optimization_status", sa.String(length=20), nullable=False, server_default="pending")
        )
        batch_op.add_column(sa.Column("optimization_error", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("optimized_video_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("optimized_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.add_column(sa.Column("pair_media_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_processing_jobs_pair_media_id", "pair_media", ["pair_media_id"], ["id"]
        )
    op.create_index(
        "ix_processing_jobs_pair_media_status", "processing_jobs", ["pair_media_id", "status"]
    )


def downgrade():
    op.drop_index("ix_processing_jobs_pair_media_status", table_name="processing_jobs")
    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.drop_constraint("fk_processing_jobs_pair_media_id", type_="foreignkey")
        batch_op.drop_column("pair_media_id")

    with op.batch_alter_table("pair_media") as batch_op:
        batch_op.drop_column("optimized_at")
        batch_op.drop_column("optimized_video_size")
        batch_op.drop_column("optimization_error")
        batch_op.drop_column("optimization_status")
        batch_op.drop_column("optimized_video_filename")
