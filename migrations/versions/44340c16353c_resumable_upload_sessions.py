"""resumable upload sessions

Revision ID: 44340c16353c
Revises: a73f2c19d8e2
Create Date: 2026-08-06 00:00:00.000000

V1 Wave 5: adds `upload_sessions`, backing a resumable chunked-upload API
that produces exactly one new single-pair Project (one image + one video)
per session on successful finalize - see models.py's UploadSession
docstring for the full lifecycle/scope-decision rationale.

Written by hand rather than via `flask db migrate` autogenerate: this
codebase's app.py runs `with app.app_context(): db.create_all()`
unconditionally at import time (a pre-existing bootstrap convenience,
unrelated to this wave) which self-heals any DB used for autogeneration to
already match current models.py before Alembic's diff ever runs, producing
an empty migration. The schema below was generated once via create_all()
on a disposable DB and cross-checked column-for-column against the
UploadSession model instead.

current_offset <= expected_total_size is enforced by an actual DB CHECK
constraint here (supported cleanly on SQLite and MySQL 8.0.16+; older MySQL
parses and ignores CHECK constraints, so this is defense-in-depth, not the
sole guard) - the authoritative enforcement is still the application-level
check in the resumable-upload chunk route before every offset update.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '44340c16353c'
down_revision = 'a73f2c19d8e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'upload_sessions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('owner_admin_id', sa.Integer(), nullable=True),
        sa.Column('purpose', sa.String(length=30), nullable=False),
        sa.Column('project_name', sa.String(length=255), nullable=True),
        sa.Column('original_image_name', sa.String(length=255), nullable=True),
        sa.Column('original_video_name', sa.String(length=255), nullable=True),
        sa.Column('image_content_type', sa.String(length=100), nullable=True),
        sa.Column('video_content_type', sa.String(length=100), nullable=True),
        sa.Column('image_size', sa.Integer(), nullable=False),
        sa.Column('video_size', sa.Integer(), nullable=False),
        sa.Column('expected_total_size', sa.Integer(), nullable=False),
        sa.Column('current_offset', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('storage_token', sa.String(length=36), nullable=False),
        sa.Column('client_checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column('computed_checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('pair_id', sa.Integer(), nullable=True),
        sa.Column('failure_code', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['owner_admin_id'], ['admins.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['pair_id'], ['project_pairs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_token'),
        sa.CheckConstraint('current_offset >= 0', name='ck_upload_session_offset_non_negative'),
        sa.CheckConstraint('current_offset <= expected_total_size', name='ck_upload_session_offset_le_total'),
        sa.CheckConstraint('image_size >= 0 AND video_size >= 0', name='ck_upload_session_sizes_non_negative'),
    )
    op.create_index('ix_upload_sessions_owner_user_id', 'upload_sessions', ['owner_user_id'])
    op.create_index('ix_upload_sessions_owner_admin_id', 'upload_sessions', ['owner_admin_id'])
    op.create_index('ix_upload_sessions_status', 'upload_sessions', ['status'])
    op.create_index('ix_upload_sessions_storage_token', 'upload_sessions', ['storage_token'])
    op.create_index('ix_upload_sessions_project_id', 'upload_sessions', ['project_id'])
    op.create_index('ix_upload_sessions_pair_id', 'upload_sessions', ['pair_id'])
    op.create_index('ix_upload_sessions_owner_user_status', 'upload_sessions', ['owner_user_id', 'status'])
    op.create_index('ix_upload_sessions_owner_admin_status', 'upload_sessions', ['owner_admin_id', 'status'])
    op.create_index('ix_upload_sessions_status_expires', 'upload_sessions', ['status', 'expires_at'])


def downgrade():
    op.drop_index('ix_upload_sessions_status_expires', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_owner_admin_status', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_owner_user_status', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_pair_id', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_project_id', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_storage_token', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_status', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_owner_admin_id', table_name='upload_sessions')
    op.drop_index('ix_upload_sessions_owner_user_id', table_name='upload_sessions')
    op.drop_table('upload_sessions')
