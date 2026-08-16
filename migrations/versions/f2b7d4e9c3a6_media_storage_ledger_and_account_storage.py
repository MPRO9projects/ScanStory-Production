"""media storage ledger and account storage entitlement (V1.1 Wave 3)

Revision ID: f2b7d4e9c3a6
Revises: e7a3f9c2b1d5
Create Date: 2026-08-16 21:00:00.000000

Wave 2 gave plans a `base_storage_bytes` ENTITLEMENT with nothing behind it.
This revision adds the accounting half:

  media_objects              authoritative per-file storage ledger (metadata
                             only - no blobs; bytes stay on the filesystem)
  users.storage_used_bytes   materialized ENFORCEMENT counter
  addon_catalog
    .storage_bytes_delta     canonical ACCOUNT_STORAGE quantity, in bytes
    ck_addon_catalog_type    widened to permit 'ACCOUNT_STORAGE'
  entitlement_transactions
    .delta_value             Integer -> BigInteger (byte deltas overflow int4)

NO FILESYSTEM SCANNING HAPPENS HERE, BY DESIGN.
Schema migration and filesystem reconciliation are separate concerns. This
revision creates an EMPTY media_objects table and leaves every existing
users.storage_used_bytes at 0. Media that predates the ledger is discovered and
recorded later by an operator running `flask reconcile-storage --apply`, which
stats real files. Backfilling invented byte counts inside Alembic - or making a
schema upgrade depend on the production data volume being mounted and complete -
would produce billing numbers nobody can audit. Zero is the honest starting
value: it means "not yet reconciled", and the CLI reports exactly that.

BigInteger for every byte column (Wave 1's audit flagged Integer's ~2.1GB cap).
PostgreSQL/SQLite compatible: server_default supplied for every NOT NULL column
so existing rows backfill in one ALTER, batch_alter_table for SQLite's
table-rebuild semantics, and the partial unique index is declared for both
dialects.
"""
from alembic import op
import sqlalchemy as sa


revision = "f2b7d4e9c3a6"
down_revision = "e7a3f9c2b1d5"
branch_labels = None
depends_on = None


CHECK_NAME = "ck_addon_catalog_type"
OLD_TYPES = ("EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE")
NEW_TYPES = OLD_TYPES + ("ACCOUNT_STORAGE",)

ACTIVE_ONLY = sa.text("status = 'ACTIVE'")


def _predicate(types):
    return "addon_type IN (" + ", ".join("'%s'" % value for value in types) + ")"


def _replace_check(allowed):
    with op.batch_alter_table("addon_catalog") as batch:
        try:
            batch.drop_constraint(CHECK_NAME, type_="check")
        except Exception:
            # A db.create_all() database may not carry the constraint under this
            # name; creating the correct one below is still the desired state.
            pass
        batch.create_check_constraint(CHECK_NAME, _predicate(allowed))


def _existing_columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _is_sqlite():
    return op.get_bind().dialect.name == "sqlite"


def upgrade():
    op.create_table(
        "media_objects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # ON DELETE SET NULL, the pattern Wave 1 established in d4e8b2c6a0f3.
        # An accounting row must OUTLIVE the project it describes: cascading it
        # away would silently free storage for bytes that may still be on disk.
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("owner_admin_id", sa.Integer(), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column(
            "project_id", sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="SET NULL", name="fk_media_objects_project_id_projects"),
            nullable=True,
        ),
        sa.Column(
            "pair_id", sa.Integer(),
            sa.ForeignKey("project_pairs.id", ondelete="SET NULL", name="fk_media_objects_pair_id_project_pairs"),
            nullable=True,
        ),
        sa.Column("media_role", sa.String(length=30), nullable=False),
        sa.Column("storage_key", sa.String(length=600), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("counts_toward_quota", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="upload"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_media_objects_owner_user_id", "media_objects", ["owner_user_id"])
    op.create_index("ix_media_objects_owner_admin_id", "media_objects", ["owner_admin_id"])
    op.create_index("ix_media_objects_project_id", "media_objects", ["project_id"])
    op.create_index("ix_media_objects_pair_id", "media_objects", ["pair_id"])
    op.create_index("ix_media_objects_status", "media_objects", ["status"])
    op.create_index("ix_media_objects_owner_status", "media_objects", ["owner_user_id", "status"])
    op.create_index("ix_media_objects_project_status", "media_objects", ["project_id", "status"])
    # DEDUP KEY: at most one ACTIVE row per storage path, so a reconciliation
    # rerun cannot double-count the same file. Partial, because superseded and
    # deleted history legitimately retains the same key.
    op.create_index(
        "uq_media_objects_active_storage_key",
        "media_objects",
        ["storage_key"],
        unique=True,
        postgresql_where=ACTIVE_ONLY,
        sqlite_where=ACTIVE_ONLY,
    )

    if "storage_used_bytes" not in _existing_columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column("storage_used_bytes", sa.BigInteger(), nullable=False, server_default="0")
            )

    if "storage_bytes_delta" not in _existing_columns("addon_catalog"):
        with op.batch_alter_table("addon_catalog") as batch:
            batch.add_column(sa.Column("storage_bytes_delta", sa.BigInteger(), nullable=True))

    _replace_check(NEW_TYPES)

    # SQLite stores every INTEGER as a 64-bit value, so the widening is already
    # true there and a batch table-rebuild would be pure risk for no effect.
    if not _is_sqlite():
        op.alter_column(
            "entitlement_transactions", "delta_value",
            existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False,
        )


def downgrade():
    if not _is_sqlite():
        op.alter_column(
            "entitlement_transactions", "delta_value",
            existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False,
        )

    bind = op.get_bind()
    offending = bind.execute(
        sa.text("SELECT COUNT(*) FROM addon_catalog WHERE addon_type = 'ACCOUNT_STORAGE'")
    ).scalar()
    if offending:
        raise RuntimeError(
            "Cannot downgrade %s: %d addon_catalog row(s) use ACCOUNT_STORAGE. "
            "Deactivate and remove them first." % (revision, offending)
        )
    _replace_check(OLD_TYPES)

    with op.batch_alter_table("addon_catalog") as batch:
        batch.drop_column("storage_bytes_delta")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("storage_used_bytes")

    op.drop_index("uq_media_objects_active_storage_key", table_name="media_objects")
    for name in (
        "ix_media_objects_project_status", "ix_media_objects_owner_status",
        "ix_media_objects_status", "ix_media_objects_pair_id",
        "ix_media_objects_project_id", "ix_media_objects_owner_admin_id",
        "ix_media_objects_owner_user_id",
    ):
        op.drop_index(name, table_name="media_objects")
    op.drop_table("media_objects")
