"""razorpay webhook events

Revision ID: ebeab1cf4ec9
Revises: 54a108a17fa7
Create Date: 2026-08-05 22:40:20.778400

V1 razorpay-webhook-reconciliation: adds razorpay_webhook_events, the
DB-backed idempotency gate for POST /webhooks/razorpay. See
models.py's RazorpayWebhookEvent docstring for exactly how
`idempotency_key` is derived (Razorpay's webhook envelope has no top-level
unique event id, so it's built from stable fields instead).

The actual replay-safety mechanism is the UNIQUE index on idempotency_key
created below - a second delivery of the same logical event fails its
INSERT at the database level (see app.py's webhook handler), it is never
just an in-app dict/set check.

This is a pure ADD (new table only); it does not touch any existing
payment/capacity table, so downgrade() only drops what this migration
itself created.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ebeab1cf4ec9'
down_revision = '54a108a17fa7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'razorpay_webhook_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('razorpay_payment_id', sa.String(length=255), nullable=True),
        sa.Column('razorpay_order_id', sa.String(length=255), nullable=True),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('processing_status', sa.String(length=20), nullable=False),
        sa.Column('payment_order_id', sa.Integer(), nullable=True),
        sa.Column('failure_code', sa.String(length=50), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('received_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    # unique=True is the real idempotency gate - see module docstring.
    op.create_index(
        'ix_razorpay_webhook_events_idempotency_key', 'razorpay_webhook_events',
        ['idempotency_key'], unique=True,
    )
    op.create_index(
        'ix_razorpay_webhook_events_payment_order_id', 'razorpay_webhook_events', ['payment_order_id']
    )
    op.create_index(
        'ix_razorpay_webhook_events_razorpay_order_id', 'razorpay_webhook_events', ['razorpay_order_id']
    )
    op.create_index(
        'ix_razorpay_webhook_events_razorpay_payment_id', 'razorpay_webhook_events', ['razorpay_payment_id']
    )


def downgrade():
    op.drop_index('ix_razorpay_webhook_events_razorpay_payment_id', table_name='razorpay_webhook_events')
    op.drop_index('ix_razorpay_webhook_events_razorpay_order_id', table_name='razorpay_webhook_events')
    op.drop_index('ix_razorpay_webhook_events_payment_order_id', table_name='razorpay_webhook_events')
    op.drop_index('ix_razorpay_webhook_events_idempotency_key', table_name='razorpay_webhook_events')
    op.drop_table('razorpay_webhook_events')
