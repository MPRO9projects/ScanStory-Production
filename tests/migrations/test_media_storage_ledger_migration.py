"""V1.1 Wave 3 - media storage ledger migration (f2b7d4e9c3a6).

Focused schema/FK checks only. The full PostgreSQL certification lane is the
project lead's, run once after this wave merges.
"""
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "e7a3f9c2b1d5"
STORAGE_REVISION = "f2b7d4e9c3a6"


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


def _migration_app(tmp_path, name):
    app = Flask(name)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)
    return app


def test_storage_revision_is_the_single_new_head():
    script = _script_directory()
    revision = script.get_revision(STORAGE_REVISION)
    assert revision.down_revision == PRIOR_REVISION
    assert script.get_current_head() == STORAGE_REVISION
    assert len(script.get_heads()) == 1


def test_upgrade_creates_the_media_ledger_and_storage_columns(tmp_path):
    app = _migration_app(tmp_path, "storage_upgrade")
    with app.app_context():
        migrate_upgrade(revision=STORAGE_REVISION)
        inspector = inspect(shared_db.engine)

        assert "media_objects" in inspector.get_table_names()
        columns = {c["name"]: c for c in inspector.get_columns("media_objects")}
        assert {
            "owner_user_id", "owner_admin_id", "project_id", "pair_id",
            "media_role", "storage_key", "size_bytes", "counts_toward_quota",
            "status", "source", "created_at", "superseded_at", "deleted_at",
            "reconciled_at",
        } <= set(columns)

        # BigInteger everywhere a byte count lives - Wave 1 flagged Integer's
        # ~2.1GB cap as a real production risk.
        assert "BIGINT" in str(columns["size_bytes"]["type"]).upper()
        user_columns = {c["name"]: c for c in inspector.get_columns("users")}
        assert "BIGINT" in str(user_columns["storage_used_bytes"]["type"]).upper()
        catalog_columns = {c["name"]: c for c in inspector.get_columns("addon_catalog")}
        assert "BIGINT" in str(catalog_columns["storage_bytes_delta"]["type"]).upper()

        # A fresh table, populated later by `flask reconcile-storage`. The
        # migration must never scan the production filesystem.
        assert shared_db.session.execute(
            shared_db.text("SELECT COUNT(*) FROM media_objects")
        ).scalar() == 0
        assert shared_db.session.execute(
            shared_db.text("SELECT COUNT(*) FROM users WHERE storage_used_bytes != 0")
        ).scalar() == 0


def test_upgrade_sets_null_on_project_and_pair_delete(tmp_path):
    """Wave 1's d4e8b2c6a0f3 pattern: an accounting row outlives its project."""
    app = _migration_app(tmp_path, "storage_fk")
    with app.app_context():
        migrate_upgrade(revision=STORAGE_REVISION)
        inspector = inspect(shared_db.engine)
        rules = {
            fk["constrained_columns"][0]: (fk["referred_table"], (fk.get("options") or {}).get("ondelete"))
            for fk in inspector.get_foreign_keys("media_objects")
            if fk.get("constrained_columns")
        }
        assert rules["project_id"] == ("projects", "SET NULL")
        assert rules["pair_id"] == ("project_pairs", "SET NULL")


def test_active_storage_key_is_unique_but_history_is_not(tmp_path):
    app = _migration_app(tmp_path, "storage_dedup")
    with app.app_context():
        migrate_upgrade(revision=STORAGE_REVISION)
        indexes = {i["name"]: i for i in inspect(shared_db.engine).get_indexes("media_objects")}
        dedup = indexes["uq_media_objects_active_storage_key"]
        assert bool(dedup["unique"])
        assert dedup["column_names"] == ["storage_key"]

        insert = shared_db.text(
            "INSERT INTO media_objects (media_role, storage_key, size_bytes, status) "
            "VALUES ('video', 'user/videos/1_0.mp4', :size, :status)"
        )
        shared_db.session.execute(insert, {"size": 10, "status": "ACTIVE"})
        # Superseded history may reuse the key (a replacement writes the same path).
        shared_db.session.execute(insert, {"size": 20, "status": "SUPERSEDED"})
        shared_db.session.commit()

        # A second ACTIVE claim on the same path is refused - this is what stops
        # a reconciliation rerun double-counting the same file.
        try:
            shared_db.session.execute(insert, {"size": 30, "status": "ACTIVE"})
            shared_db.session.commit()
            raise AssertionError("duplicate ACTIVE storage_key should be rejected")
        except Exception:
            shared_db.session.rollback()
