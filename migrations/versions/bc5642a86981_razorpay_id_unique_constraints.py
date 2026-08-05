"""razorpay id unique constraints

Revision ID: bc5642a86981
Revises: 3914ece79b88
Create Date: 2026-08-05 00:00:00.000000

V1 Phase 2 (payment idempotency + capacity), area 2: enforce at the DB level
that razorpay_order_id and razorpay_payment_id are unique across
payment_orders. Both columns are already effectively unique in practice (one
order created per checkout, one payment id per successful charge) but
nothing previously stopped a bug or a replayed/duplicated write from
inserting a second row with the same value.

Both columns stay nullable=True (no data migration needed) and use a PLAIN
unique index rather than a partial/conditional one. This is deliberate, not
an oversight: under standard SQL NULL comparison semantics (NULL is never
equal to NULL), a plain UNIQUE index in SQLite, MySQL, AND Postgres already
permits an unlimited number of NULL rows while still enforcing uniqueness
among the non-NULL values. So "unique only when set" needs no
dialect-specific partial-index syntax at all here - a plain unique index is
already correct and portable across every dialect this project targets
(SQLite for tests/dev, MySQL in production, per the same dialect gating
convention as app.py's _supports_row_level_locking() /
migrations/env.py's _needs_batch_mode()).

Safety: before touching the index, upgrade() runs a duplicate-preflight
query. If any existing duplicate (non-NULL) razorpay_order_id or
razorpay_payment_id values are found, it raises and the migration aborts
without altering anything - it never silently deletes or merges rows. (Verify
this behavior yourself in a disposable DB by inserting two payment_orders
rows with the same razorpay_payment_id before running `flask db upgrade` -
the RuntimeError below fires; with duplicates removed, upgrade proceeds and
a genuine duplicate INSERT afterward is rejected by the DB itself.)
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bc5642a86981'
down_revision = '3914ece79b88'
branch_labels = None
depends_on = None


def _find_duplicate_values(conn, table, column):
    return conn.execute(sa.text(
        f"SELECT {column}, COUNT(*) AS c FROM {table} "
        f"WHERE {column} IS NOT NULL GROUP BY {column} HAVING COUNT(*) > 1"
    )).fetchall()


def upgrade():
    conn = op.get_bind()

    dup_order_ids = _find_duplicate_values(conn, 'payment_orders', 'razorpay_order_id')
    dup_payment_ids = _find_duplicate_values(conn, 'payment_orders', 'razorpay_payment_id')
    if dup_order_ids or dup_payment_ids:
        raise RuntimeError(
            "Refusing to add unique constraints on payment_orders: duplicate "
            f"razorpay_order_id values found: {[row[0] for row in dup_order_ids]}; "
            f"duplicate razorpay_payment_id values found: {[row[0] for row in dup_payment_ids]}. "
            "Resolve these duplicates manually (never auto-merge/delete payment "
            "records) and re-run this migration."
        )

    # Index changes don't require table rebuilds on any of our target
    # dialects, so no batch_alter_table/render_as_batch is needed here.
    op.drop_index('ix_payment_orders_razorpay_order_id', table_name='payment_orders')
    op.create_index(
        'ix_payment_orders_razorpay_order_id', 'payment_orders', ['razorpay_order_id'], unique=True
    )
    op.drop_index('ix_payment_orders_razorpay_payment_id', table_name='payment_orders')
    op.create_index(
        'ix_payment_orders_razorpay_payment_id', 'payment_orders', ['razorpay_payment_id'], unique=True
    )


def downgrade():
    op.drop_index('ix_payment_orders_razorpay_payment_id', table_name='payment_orders')
    op.create_index(
        'ix_payment_orders_razorpay_payment_id', 'payment_orders', ['razorpay_payment_id'], unique=False
    )
    op.drop_index('ix_payment_orders_razorpay_order_id', table_name='payment_orders')
    op.create_index(
        'ix_payment_orders_razorpay_order_id', 'payment_orders', ['razorpay_order_id'], unique=False
    )
