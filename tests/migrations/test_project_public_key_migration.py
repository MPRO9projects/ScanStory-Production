from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import upgrade as migrate_upgrade
from flask_migrate import downgrade as migrate_downgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "f6a8d0c2e4b9"
PUBLIC_KEY_REVISION = "a4f2c8d9e1b7"


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


@pytest.fixture()
def bare_migration_app(tmp_path):
    created = []

    def _make(name):
        app = Flask(f"project_public_key_migration_test_{name}")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / name).as_posix()}"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app.secret_key = "migration-test-only"
        shared_db.init_app(app)
        Migrate(app, shared_db, directory=MIGRATIONS_DIR)
        created.append(app)
        return app

    yield _make

    for app in created:
        with app.app_context():
            shared_db.session.remove()
            shared_db.engine.dispose()


def test_project_public_key_migration_revision_exists():
    script = _script_directory().get_revision(PUBLIC_KEY_REVISION)
    assert script.revision == PUBLIC_KEY_REVISION
    assert script.down_revision == PRIOR_REVISION


def test_project_public_key_upgrade_backfills_legacy_projects(bare_migration_app):
    app = bare_migration_app("project_public_key_upgrade.db")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text("INSERT INTO projects (name, is_active) VALUES ('legacy one', 1)"))
        shared_db.session.execute(text("INSERT INTO projects (name, is_active) VALUES ('legacy two', 1)"))
        shared_db.session.commit()

        migrate_upgrade(revision=PUBLIC_KEY_REVISION)

        columns = {c["name"]: c for c in inspect(shared_db.engine).get_columns("projects")}
        indexes = {idx["name"] for idx in inspect(shared_db.engine).get_indexes("projects")}
        constraints = {uc["name"] for uc in inspect(shared_db.engine).get_unique_constraints("projects")}
        rows = shared_db.session.execute(text("SELECT id, public_key FROM projects ORDER BY id")).fetchall()

    keys = [row.public_key for row in rows]
    assert "public_key" in columns
    assert columns["public_key"]["nullable"] is False
    assert "ix_projects_public_key" in indexes
    assert "uq_projects_public_key" in constraints
    assert len(keys) == 2
    assert len(set(keys)) == 2
    assert all(key.startswith("prj_") for key in keys)
    assert all(row.public_key != str(row.id) for row in rows)
    assert all(row.public_key != f"prj_{row.id}" for row in rows)


def test_project_public_key_unique_constraint_rejects_duplicate(bare_migration_app):
    app = bare_migration_app("project_public_key_unique.db")
    with app.app_context():
        migrate_upgrade(revision=PUBLIC_KEY_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO projects (name, is_active, public_key) VALUES ('one', 1, 'prj_duplicate')"
        ))
        shared_db.session.commit()

        with pytest.raises(Exception):
            shared_db.session.execute(text(
                "INSERT INTO projects (name, is_active, public_key) VALUES ('two', 1, 'prj_duplicate')"
            ))
            shared_db.session.commit()
        shared_db.session.rollback()


def test_project_public_key_downgrade_removes_only_public_key_field(bare_migration_app):
    app = bare_migration_app("project_public_key_downgrade.db")
    with app.app_context():
        migrate_upgrade(revision=PUBLIC_KEY_REVISION)
        columns_before = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

        migrate_downgrade(revision=PRIOR_REVISION)

        columns_after = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

    assert columns_before - columns_after == {"public_key"}
