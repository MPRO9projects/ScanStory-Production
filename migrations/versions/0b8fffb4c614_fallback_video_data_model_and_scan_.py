"""fallback video data model and scan events

Revision ID: 0b8fffb4c614
Revises: 44340c16353c
Create Date: 2026-08-06 16:34:16.465377

V1 Wave 6: adds project-level/pair-level fallback-video support and a
dedicated `scan_events` table for fallback/analytics event classification
(pair_fallback_view, project_fallback_view, recognition_timeout,
camera_unavailable). ScanLog is completely untouched by this migration -
see ScanEvent's docstring in models.py for why a new table was chosen over
extending ScanLog (ScanLog's UniqueConstraint(user_id, scan_session_id) is
one-row-per-session, structurally wrong for events that can repeat within a
session; several existing ScanLog aggregates also count rows without an
is_successful filter and would have silently absorbed fallback events).
Every pre-migration ScanLog row is therefore unaffected and still means
exactly what it always meant.

Generated via `flask db migrate` against a disposable SQLite DB bound
directly to `models.db`/Migrate (bare Flask app, no app.py import - the
same documented workaround from 44340c16353c's header, since app.py's
unconditional `db.create_all()` would otherwise self-heal the diff away to
nothing), then hand-trimmed:
- Dropped an unrelated autogenerate-detected diff on
  `upload_sessions.ix_upload_sessions_storage_token` (unique=False ->
  True) - pre-existing drift between that Wave 5 migration and its model,
  not something this wave touches or fixes.
- Added an explicit CHECK constraint mirroring the `event_type` enum
  enforced at the ORM level (`@validates` in models.py) - defense-in-depth,
  not the sole guard, same rationale 44340c16353c uses for its own CHECK
  constraints.

`projects.fallback_pair_id` uses `use_alter=True` (see models.py) because
it creates a two-table cycle with `project_pairs.project_id`'s existing FK
back to `projects.id` - the column/FK is therefore added to `projects` in a
separate step after `project_pairs` already exists, which it always does
here since this migration only ever runs after the baseline.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0b8fffb4c614'
down_revision = '44340c16353c'
branch_labels = None
depends_on = None


SCAN_EVENT_TYPES = (
    "pair_fallback_view",
    "project_fallback_view",
    "recognition_timeout",
    "camera_unavailable",
)


def upgrade():
    op.create_table(
        'scan_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('pair_id', sa.Integer(), nullable=True),
        sa.Column('event_type', sa.String(length=30), nullable=False),
        sa.Column('scan_session_id', sa.String(length=100), nullable=True),
        sa.Column('client_event_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['pair_id'], ['project_pairs.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "event_type IN ('" + "','".join(SCAN_EVENT_TYPES) + "')",
            name='ck_scan_events_event_type',
        ),
    )
    op.create_index('ix_scan_events_project_id', 'scan_events', ['project_id'])
    op.create_index('ix_scan_events_pair_id', 'scan_events', ['pair_id'])
    op.create_index('ix_scan_events_event_type', 'scan_events', ['event_type'])
    op.create_index('ix_scan_events_scan_session_id', 'scan_events', ['scan_session_id'])
    op.create_index('ix_scan_events_client_event_id', 'scan_events', ['client_event_id'], unique=True)

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fallback_pair_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_projects_fallback_pair_id', 'project_pairs', ['fallback_pair_id'], ['id'], use_alter=True)


def downgrade():
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_constraint('fk_projects_fallback_pair_id', type_='foreignkey')
        batch_op.drop_column('fallback_pair_id')

    op.drop_index('ix_scan_events_client_event_id', table_name='scan_events')
    op.drop_index('ix_scan_events_scan_session_id', table_name='scan_events')
    op.drop_index('ix_scan_events_event_type', table_name='scan_events')
    op.drop_index('ix_scan_events_pair_id', table_name='scan_events')
    op.drop_index('ix_scan_events_project_id', table_name='scan_events')
    op.drop_table('scan_events')
