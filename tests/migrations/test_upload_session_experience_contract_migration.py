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
PRIOR_REVISION = "d2a4b6c8e0f1"
UPLOAD_CONTRACT_REVISION = "e5f6a7b8c9d0"


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


def test_upload_session_experience_contract_is_on_the_single_head_chain():
    # Asserts placement in the chain, not "is the head": later phases layer
    # further revisions on top and there must still be exactly one head.
    script = _script_directory()
    revision = script.get_revision(UPLOAD_CONTRACT_REVISION)
    assert len(script.get_heads()) == 1
    assert revision.down_revision == PRIOR_REVISION
    ancestry = {r.revision for r in script.iterate_revisions(script.get_current_head(), "base")}
    assert UPLOAD_CONTRACT_REVISION in ancestry


def test_upload_session_experience_contract_upgrade_backfills_existing_sessions(tmp_path):
    app = _migration_app(tmp_path, "upload_contract_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO upload_sessions "
            "(purpose, project_name, image_size, video_size, expected_total_size, current_offset, status, storage_token, expires_at) "
            "VALUES ('project_pair', 'legacy', 10, 20, 30, 0, 'active', '11111111-1111-4111-8111-111111111111', CURRENT_TIMESTAMP)"
        ))
        shared_db.session.commit()

        migrate_upgrade(revision=UPLOAD_CONTRACT_REVISION)

        cols = {c["name"] for c in inspect(shared_db.engine).get_columns("upload_sessions")}
        row = shared_db.session.execute(text(
            "SELECT experience_type, playback_mode FROM upload_sessions"
        )).fetchone()

    assert {"experience_type", "playback_mode"} <= cols
    assert row == ("image_video", "tracked_overlay")


def test_upload_session_experience_contract_downgrade_drops_only_contract_columns(tmp_path):
    app = _migration_app(tmp_path, "upload_contract_downgrade")
    with app.app_context():
        migrate_upgrade(revision=UPLOAD_CONTRACT_REVISION)
        cols_before = {c["name"] for c in inspect(shared_db.engine).get_columns("upload_sessions")}

        migrate_downgrade(revision=PRIOR_REVISION)

        cols_after = {c["name"] for c in inspect(shared_db.engine).get_columns("upload_sessions")}

    assert cols_before - cols_after == {"experience_type", "playback_mode"}
    assert "client_checksum_sha256" in cols_after
