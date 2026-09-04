"""creator integrity hashes and idempotency key

Revision ID: 67dd06eecbd2
Revises: 6f62644e4996
Create Date: 2026-08-28 18:29:07.167338

Local Creator Integrity & Direct QR UX pass (2026-08-28). Adds only the three
columns this pass actually needs. Autogenerate also proposed a batch of
unrelated pre-existing schema drift (unique-constraint renames on
addon_catalog/addon_purchases/upload_sessions, a new experiences FK, a
processing_jobs index) that predates this pass and is intentionally left out
of this revision - touching it here would be an unrelated, unreviewed change
riding along with this one.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '67dd06eecbd2'
down_revision = '6f62644e4996'
branch_labels = None
depends_on = None


def upgrade():
    # ProjectPair canonical target identity - duplicate-target detection.
    op.add_column('project_pairs', sa.Column('image_hash', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_project_pairs_image_hash'), 'project_pairs', ['image_hash'], unique=False)
    op.create_index('uq_project_pair_image_hash', 'project_pairs', ['project_id', 'image_hash'], unique=True)

    # PairMedia canonical video identity - duplicate-video race guard.
    op.add_column('pair_media', sa.Column('video_hash', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_pair_media_video_hash'), 'pair_media', ['video_hash'], unique=False)
    op.create_index('uq_pair_media_video_hash', 'pair_media', ['pair_id', 'video_hash'], unique=True)

    # Project creation idempotency - reuses the client's existing per-submission upload_id.
    op.add_column('projects', sa.Column('creation_idempotency_key', sa.String(length=80), nullable=True))
    op.create_index(op.f('ix_projects_creation_idempotency_key'), 'projects', ['creation_idempotency_key'], unique=True)


def downgrade():
    op.drop_index(op.f('ix_projects_creation_idempotency_key'), table_name='projects')
    op.drop_column('projects', 'creation_idempotency_key')

    op.drop_index('uq_pair_media_video_hash', table_name='pair_media')
    op.drop_index(op.f('ix_pair_media_video_hash'), table_name='pair_media')
    op.drop_column('pair_media', 'video_hash')

    op.drop_index('uq_project_pair_image_hash', table_name='project_pairs')
    op.drop_index(op.f('ix_project_pairs_image_hash'), table_name='project_pairs')
    op.drop_column('project_pairs', 'image_hash')
