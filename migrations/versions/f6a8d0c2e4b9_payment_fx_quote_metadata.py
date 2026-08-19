"""payment fx quote metadata

Revision ID: f6a8d0c2e4b9
Revises: e9b4d7a2c815
Create Date: 2026-08-19 16:45:00.000000

Adds nullable quote-lock fields for future non-INR checkout support. Existing
INR-only historical rows are deliberately left NULL rather than backfilled with
invented FX metadata.
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a8d0c2e4b9"
down_revision = "e9b4d7a2c815"
branch_labels = None
depends_on = None


QUOTE_COLUMNS = (
    sa.Column("base_amount", sa.Float(), nullable=True),
    sa.Column("base_currency", sa.String(length=3), nullable=True),
    sa.Column("quoted_amount", sa.Float(), nullable=True),
    sa.Column("quoted_currency", sa.String(length=3), nullable=True),
    sa.Column("fx_rate", sa.Float(), nullable=True),
    sa.Column("fx_rate_source", sa.String(length=80), nullable=True),
    sa.Column("fx_rate_timestamp", sa.DateTime(), nullable=True),
)


def _add_quote_columns(table_name):
    for column in QUOTE_COLUMNS:
        op.add_column(table_name, column.copy())


def _drop_quote_columns(table_name):
    for column in reversed(QUOTE_COLUMNS):
        op.drop_column(table_name, column.name)


def upgrade():
    _add_quote_columns("payment_orders")
    _add_quote_columns("addon_purchases")


def downgrade():
    _drop_quote_columns("addon_purchases")
    _drop_quote_columns("payment_orders")
