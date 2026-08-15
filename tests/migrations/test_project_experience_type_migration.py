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
PRIOR_REVISION = "f4a8c2b91d70"
EXPERIENCE_REVISION = "b7c9d2e4f6a1"


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


def test_project_experience_type_migration_revision_exists():
    script = _script_directory().get_revision(EXPERIENCE_REVISION)
    assert script.revision == EXPERIENCE_REVISION
    assert script.down_revision == PRIOR_REVISION


def test_project_experience_type_upgrade_backfills_legacy_projects(tmp_path):
    app = Flask("project_experience_type_upgrade")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'upgrade.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)

    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text("INSERT INTO projects (name, is_active) VALUES ('legacy', 1)"))
        shared_db.session.commit()

        migrate_upgrade(revision=EXPERIENCE_REVISION)

        project_cols = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}
        pair_cols = {c["name"]: c for c in inspect(shared_db.engine).get_columns("project_pairs")}
        rows = shared_db.session.execute(text("SELECT experience_type FROM projects")).fetchall()

    assert "experience_type" in project_cols
    assert rows == [("image_video",)]
    assert pair_cols["image_filename"]["nullable"] is True


def test_project_experience_type_downgrade_removes_only_new_project_field_and_restores_pair_nullability(tmp_path):
    app = Flask("project_experience_type_downgrade")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'downgrade.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)

    with app.app_context():
        migrate_upgrade(revision=EXPERIENCE_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO projects (name, is_active, experience_type) VALUES ('direct', 1, 'direct_qr')"
        ))
        shared_db.session.commit()
        project_id = shared_db.session.execute(text("SELECT id FROM projects")).fetchone()[0]
        shared_db.session.execute(text(
            "INSERT INTO project_pairs (project_id, pair_index, image_filename, video_filename) "
            "VALUES (:pid, 0, NULL, 'direct.mp4')"
        ), {"pid": project_id})
        shared_db.session.commit()

        migrate_downgrade(revision=PRIOR_REVISION)

        project_cols = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}
        pair_cols = {c["name"]: c for c in inspect(shared_db.engine).get_columns("project_pairs")}
        image_filename = shared_db.session.execute(text("SELECT image_filename FROM project_pairs")).fetchone()[0]

    assert "experience_type" not in project_cols
    assert pair_cols["image_filename"]["nullable"] is False
    assert image_filename == ""
