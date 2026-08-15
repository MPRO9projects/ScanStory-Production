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
PRIOR_REVISION = "c8d1e2f3a4b5"
DOMAIN_REVISION = "d2a4b6c8e0f1"


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


def test_domain_ownership_revision_is_current_head():
    script = _script_directory()
    revision = script.get_revision(DOMAIN_REVISION)
    assert script.get_current_head() == DOMAIN_REVISION
    assert revision.down_revision == PRIOR_REVISION


def test_domain_ownership_upgrade_backfills_users_projects_and_legacy_coverage(tmp_path):
    app = _migration_app(tmp_path, "domain_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO users (email, password_hash, is_verified, subscription_status) "
            "VALUES ('owner@example.com', 'hash', 1, 'active')"
        ))
        owner_id = shared_db.session.execute(text("SELECT id FROM users WHERE email='owner@example.com'")).fetchone()[0]
        shared_db.session.execute(text(
            "INSERT INTO projects (name, owner_user_id, is_active, experience_type, playback_mode) "
            "VALUES ('active legacy', :owner_id, 1, 'image_video', 'tracked_overlay'), "
            "('inactive legacy', :owner_id, 0, 'image_video', 'tracked_overlay'), "
            "('admin legacy', NULL, 1, 'image_video', 'tracked_overlay')"
        ), {"owner_id": owner_id})
        shared_db.session.commit()

        migrate_upgrade(revision=DOMAIN_REVISION)

        user_row = shared_db.session.execute(text("SELECT account_type FROM users WHERE id=:id"), {"id": owner_id}).fetchone()
        project_rows = shared_db.session.execute(text(
            "SELECT name, created_by_user_id, current_owner_user_id FROM projects ORDER BY id"
        )).fetchall()
        coverage_rows = shared_db.session.execute(text(
            "SELECT p.name, c.source_type, c.coverage_end "
            "FROM projects p LEFT JOIN project_service_coverages c ON c.project_id = p.id "
            "ORDER BY p.id"
        )).fetchall()

    assert user_row[0] == "INDIVIDUAL"
    assert project_rows[0] == ("active legacy", owner_id, owner_id)
    assert project_rows[1] == ("inactive legacy", owner_id, owner_id)
    assert project_rows[2] == ("admin legacy", None, None)
    assert coverage_rows[0][1:] == ("LEGACY_COMPATIBILITY", None)
    assert coverage_rows[1][1:] == (None, None)
    assert coverage_rows[2][1:] == ("LEGACY_COMPATIBILITY", None)


def test_domain_ownership_downgrade_drops_only_domain_additions(tmp_path):
    app = _migration_app(tmp_path, "domain_downgrade")
    with app.app_context():
        migrate_upgrade(revision=DOMAIN_REVISION)
        tables_before = set(inspect(shared_db.engine).get_table_names())
        user_cols_before = {c["name"] for c in inspect(shared_db.engine).get_columns("users")}
        project_cols_before = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

        migrate_downgrade(revision=PRIOR_REVISION)

        tables_after = set(inspect(shared_db.engine).get_table_names())
        user_cols_after = {c["name"] for c in inspect(shared_db.engine).get_columns("users")}
        project_cols_after = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

    assert tables_before - tables_after == {
        "project_ownership_transfers",
        "project_ownership_claims",
        "project_service_coverages",
    }
    assert user_cols_before - user_cols_after == {"account_type"}
    assert project_cols_before - project_cols_after == {
        "created_by_user_id",
        "current_owner_user_id",
        "manager_vendor_user_id",
        "beneficiary_user_id",
    }
    assert "experience_type" in project_cols_after
    assert "playback_mode" in project_cols_after
