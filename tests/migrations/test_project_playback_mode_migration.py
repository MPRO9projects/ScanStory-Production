from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import upgrade as migrate_upgrade
from flask_migrate import downgrade as migrate_downgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "b7c9d2e4f6a1"
PLAYBACK_REVISION = "c8d1e2f3a4b5"


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


def test_project_playback_mode_migration_revision_exists():
    script = _script_directory()
    revision = script.get_revision(PLAYBACK_REVISION)
    assert revision.revision == PLAYBACK_REVISION
    assert revision.down_revision == PRIOR_REVISION


def test_project_playback_mode_upgrade_backfills_by_experience_type(tmp_path):
    app = _migration_app(tmp_path, "playback_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO projects (name, is_active, experience_type) VALUES "
            "('legacy image', 1, 'image_video'), ('direct', 1, 'direct_qr')"
        ))
        shared_db.session.commit()

        migrate_upgrade(revision=PLAYBACK_REVISION)

        project_cols = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}
        rows = shared_db.session.execute(text(
            "SELECT experience_type, playback_mode FROM projects ORDER BY id"
        )).fetchall()

    assert "playback_mode" in project_cols
    assert rows == [("image_video", "tracked_overlay"), ("direct_qr", "direct")]


def test_project_playback_mode_downgrade_drops_only_playback_column(tmp_path):
    app = _migration_app(tmp_path, "playback_downgrade")
    with app.app_context():
        migrate_upgrade(revision=PLAYBACK_REVISION)
        cols_before = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

        migrate_downgrade(revision=PRIOR_REVISION)

        cols_after = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

    assert cols_before - cols_after == {"playback_mode"}
    assert "experience_type" in cols_after
