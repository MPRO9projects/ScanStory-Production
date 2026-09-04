"""Migration tests for Fast Video Phase 1: PairMedia derivative fields +
ProcessingJob.pair_media_id.

Mirrors tests/migrations/test_pair_media_migration.py's pattern exactly:
a throwaway Flask app + sqlite db per test, driven directly through
Flask-Migrate's upgrade()/downgrade() against specific revisions - no need
to import app.py or its runtime-config validation at all.
"""
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_HEAD = "f53b3c212bba"
NEW_REVISION = "6f62644e4996"

NEW_PAIR_MEDIA_COLUMNS = {
    "optimized_video_filename", "optimization_status", "optimization_error",
    "optimized_video_size", "optimized_at",
}


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


def test_revision_is_the_single_new_head_on_top_of_prior_head():
    script = _script_directory()
    revision = script.get_revision(NEW_REVISION)
    assert revision.down_revision == PRIOR_HEAD
    assert len(script.get_heads()) == 1
    assert script.get_current_head() == NEW_REVISION


# ===========================================================================
# 1: migration upgrade
# ===========================================================================
def test_upgrade_adds_pair_media_derivative_columns(tmp_path):
    app = _migration_app(tmp_path, "fastvideo_upgrade_pair_media")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        columns_before = {c["name"] for c in inspector.get_columns("pair_media")}
        assert not (NEW_PAIR_MEDIA_COLUMNS & columns_before)

        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        columns_after = {c["name"] for c in inspector.get_columns("pair_media")}
        assert NEW_PAIR_MEDIA_COLUMNS <= columns_after


def test_upgrade_adds_processing_jobs_pair_media_id(tmp_path):
    app = _migration_app(tmp_path, "fastvideo_upgrade_processing_jobs")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        columns = {c["name"] for c in inspector.get_columns("processing_jobs")}
        assert "pair_media_id" in columns
        index_names = {ix["name"] for ix in inspector.get_indexes("processing_jobs")}
        assert "ix_processing_jobs_pair_media_status" in index_names


# ===========================================================================
# 2: downgrade / re-upgrade
# ===========================================================================
def test_downgrade_removes_exactly_what_upgrade_added(tmp_path):
    app = _migration_app(tmp_path, "fastvideo_downgrade")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        migrate_downgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        pair_media_columns = {c["name"] for c in inspector.get_columns("pair_media")}
        assert not (NEW_PAIR_MEDIA_COLUMNS & pair_media_columns)
        processing_jobs_columns = {c["name"] for c in inspector.get_columns("processing_jobs")}
        assert "pair_media_id" not in processing_jobs_columns
        # Neither direction touches project_pairs at all.
        project_pairs_columns = {c["name"] for c in inspector.get_columns("project_pairs")}
        assert "optimized_video_filename" not in project_pairs_columns


def test_upgrade_is_rerunnable_after_downgrade(tmp_path):
    app = _migration_app(tmp_path, "fastvideo_reupgrade")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        migrate_downgrade(revision=PRIOR_HEAD)
        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        columns = {c["name"] for c in inspector.get_columns("pair_media")}
        assert NEW_PAIR_MEDIA_COLUMNS <= columns


# ===========================================================================
# 3: old PairMedia rows remain valid after migration
# ===========================================================================
def test_existing_pair_media_row_gets_truthful_pending_defaults(tmp_path):
    app = _migration_app(tmp_path, "fastvideo_existing_row")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.engine.connect()
        conn.execute(text(
            "INSERT INTO projects (name, public_key, created_at, updated_at) "
            "VALUES ('P', 'pk-fastvideo-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        project_id = conn.execute(text("SELECT id FROM projects WHERE public_key='pk-fastvideo-1'")).scalar_one()
        conn.execute(text(
            "INSERT INTO project_pairs (project_id, pair_index, video_filename, created_at, updated_at) "
            "VALUES (:pid, 0, 'v.mp4', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"pid": project_id})
        pair_id = conn.execute(text("SELECT id FROM project_pairs WHERE project_id=:pid"), {"pid": project_id}).scalar_one()
        conn.execute(text(
            "INSERT INTO pair_media (pair_id, video_filename, sort_order, is_default, created_at, updated_at) "
            "VALUES (:pair_id, 'v.mp4', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"pair_id": pair_id})
        conn.commit()
        conn.close()

        migrate_upgrade(revision=NEW_REVISION)

        conn = shared_db.engine.connect()
        row = conn.execute(text(
            "SELECT video_filename, optimization_status, optimized_video_filename, "
            "optimized_video_size, optimized_at, optimization_error FROM pair_media WHERE pair_id=:pid"
        ), {"pid": pair_id}).fetchone()
        conn.close()
        # video_filename (the original) is completely untouched by the migration.
        assert row[0] == "v.mp4"
        # No backfill required - a pre-existing row is truthfully "pending".
        assert row[1] == "pending"
        assert row[2] is None
        assert row[3] is None
        assert row[4] is None
        assert row[5] is None
