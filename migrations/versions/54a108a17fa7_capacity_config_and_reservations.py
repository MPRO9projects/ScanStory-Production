"""capacity config and payment reservations

Revision ID: 54a108a17fa7
Revises: bc5642a86981
Create Date: 2026-08-05 00:05:00.000000

V1 Phase 2, areas 3-5: durable global paid-account capacity config plus the
reservation table backing atomic capacity gating on new checkouts.

capacity_config is a single-row table (id=1), seeded here with
configured_limit=25 / enabled=True / consumed_count=0. See models.py's
CapacityConfig docstring for the exact invariant app.py maintains:

    consumed_count == count(payment_reservations rows with
                             status in ('reserved', 'activated'))

payment_reservations.status lifecycle: reserved -> activated | released |
expired. See models.py's PaymentReservation docstring for what triggers each
transition.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '54a108a17fa7'
down_revision = 'bc5642a86981'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'capacity_config',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('configured_limit', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('consumed_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'payment_reservations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('payment_order_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reserved_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_payment_reservations_user_id', 'payment_reservations', ['user_id'])
    op.create_index('ix_payment_reservations_payment_order_id', 'payment_reservations', ['payment_order_id'])
    op.create_index('ix_payment_reservations_status', 'payment_reservations', ['status'])

    # Seed the single config row via a typed insert (not raw SQL literals) so
    # the boolean value is rendered correctly per-dialect (0/1 on
    # SQLite/MySQL, TRUE on Postgres) rather than assuming one dialect's
    # literal syntax.
    capacity_config_table = sa.table(
        'capacity_config',
        sa.column('id', sa.Integer),
        sa.column('configured_limit', sa.Integer),
        sa.column('enabled', sa.Boolean),
        sa.column('consumed_count', sa.Integer),
    )
    op.bulk_insert(capacity_config_table, [
        {'id': 1, 'configured_limit': 25, 'enabled': True, 'consumed_count': 0},
    ])


def downgrade():
    op.drop_index('ix_payment_reservations_status', table_name='payment_reservations')
    op.drop_index('ix_payment_reservations_payment_order_id', table_name='payment_reservations')
    op.drop_index('ix_payment_reservations_user_id', table_name='payment_reservations')
    op.drop_table('payment_reservations')
    op.drop_table('capacity_config')
