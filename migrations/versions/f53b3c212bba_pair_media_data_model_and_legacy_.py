"""pair media data model and legacy backfill (Issue 3E-B)

Revision ID: f53b3c212bba
Revises: 6e2ed8acdbcf
Create Date: 2026-08-22 15:10:00.000000

Introduces PairMedia - the child video catalog for the future one-target/
many-videos capability - ALONGSIDE the existing ProjectPair video columns,
which remain fully authoritative for the current runtime in this phase.
Nothing reads pair_media yet: upload writes ProjectPair.video_filename,
scanner reads ProjectPair.video_filename, serve_video serves it. This
migration only gives every existing pair a parallel, backward-compatible
representation for later phases to build on.

  pair_media
    pair_id                FK -> project_pairs.id, NOT NULL, indexed
    video_filename         same file the legacy column already points at -
                            METADATA ONLY, no file is copied or re-uploaded
    original_video_name / video_size   copied from the legacy pair
    sort_order              0 for every backfilled row (single video)
    is_default               True for every backfilled row (the only video)

DEFAULT-MEDIA ENFORCEMENT: a partial unique index on pair_id WHERE
is_default - at most one default row per pair, at the DB level. Both
PostgreSQL and SQLite support a WHERE-qualified unique index, so this is one
declaration, not per-database logic.

BACKFILL: one PairMedia row per ProjectPair that has a non-null, non-empty
video_filename - covers user-owned, admin-owned, Direct QR and
image-recognition pairs identically, since none of those distinctions live
on ProjectPair's video columns. A pair with no video (nullable/legacy edge
case, video_filename is NOT NULL today but this guards it anyway) gets no
row. Two pairs sharing the same filename each get their own independent
PairMedia row, keyed by pair_id - never merged, never deduplicated across
pairs. No MediaObject/storage-accounting row is created or modified - this
is a relationship/catalog record over an already-accounted-for file, not a
second upload.

Reversible: downgrade drops pair_media only. ProjectPair and its video
columns are never touched by either direction.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "f53b3c212bba"
down_revision = "6e2ed8acdbcf"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pair_media",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("project_pairs.id"), nullable=False),
        sa.Column("video_filename", sa.String(length=255), nullable=False),
        sa.Column("original_video_name", sa.String(length=255), nullable=True),
        sa.Column("video_size", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pair_media_pair_id", "pair_media", ["pair_id"])
    op.create_index(
        "uq_pair_media_one_default_per_pair",
        "pair_media",
        ["pair_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
        sqlite_where=sa.text("is_default = 1"),
    )

    project_pairs = sa.table(
        "project_pairs",
        sa.column("id", sa.Integer()),
        sa.column("video_filename", sa.String()),
        sa.column("original_video_name", sa.String()),
        sa.column("video_size", sa.Integer()),
    )
    pair_media = sa.table(
        "pair_media",
        sa.column("pair_id", sa.Integer()),
        sa.column("video_filename", sa.String()),
        sa.column("original_video_name", sa.String()),
        sa.column("video_size", sa.Integer()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_default", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.select(
            project_pairs.c.id,
            project_pairs.c.video_filename,
            project_pairs.c.original_video_name,
            project_pairs.c.video_size,
        ).where(
            project_pairs.c.video_filename.isnot(None),
            project_pairs.c.video_filename != "",
        )
    ).fetchall()

    now = datetime.utcnow()
    if rows:
        op.bulk_insert(
            pair_media,
            [
                {
                    "pair_id": row.id,
                    "video_filename": row.video_filename,
                    "original_video_name": row.original_video_name,
                    "video_size": row.video_size,
                    "sort_order": 0,
                    "is_default": True,
                    "created_at": now,
                    "updated_at": now,
                }
                for row in rows
            ],
        )


def downgrade():
    op.drop_index("uq_pair_media_one_default_per_pair", table_name="pair_media")
    op.drop_index("ix_pair_media_pair_id", table_name="pair_media")
    op.drop_table("pair_media")
