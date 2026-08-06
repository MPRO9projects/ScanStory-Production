"""Migration checks for 0b8fffb4c614 (fallback video data model + scan_events
analytics table), added by v1/wave-6-fallback-data-analytics on top of the
44340c16353c head.

Uses the same bare-Flask-app-bound-to-shared-db pattern as
tests/migrations/test_resumable_upload_migration.py's bare_migration_app
fixture (duplicated locally rather than imported, matching that file's own
convention) - deliberately never imports app.py, whose module-level
`db.create_all()` would otherwise create every table (including this one)
before Alembic's own upgrade ever runs.
"""
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
PRIOR_HEAD = "44340c16353c"


def _current_head_revision():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture()
def bare_migration_app(tmp_path):
    created = []

    def _make(name):
        app = Flask(f"fallback_analytics_migration_test_{name}")
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


def test_exactly_one_alembic_head(bare_migration_app):
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    assert heads[0] == _current_head_revision()


def test_clean_database_upgrade_includes_scan_events_and_fallback_column(bare_migration_app):
    app = bare_migration_app("clean_scan_events.db")
    with app.app_context():
        migrate_upgrade()
        tables = set(inspect(shared_db.engine).get_table_names())
        assert "scan_events" in tables
        project_cols = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}
        assert "fallback_pair_id" in project_cols
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_upgrade_from_prior_head_reaches_new_head(bare_migration_app):
    app = bare_migration_app("from_prior_head.db")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        tables_before = set(inspect(shared_db.engine).get_table_names())
        assert "scan_events" not in tables_before

        migrate_upgrade()  # to new head
        tables_after = set(inspect(shared_db.engine).get_table_names())
        assert "scan_events" in tables_after
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_repeated_upgrade_is_idempotent(bare_migration_app):
    app = bare_migration_app("repeat_scan_events.db")
    with app.app_context():
        migrate_upgrade()
        tables_first = set(inspect(shared_db.engine).get_table_names())
        migrate_upgrade()  # must be a no-op, not an error
        tables_second = set(inspect(shared_db.engine).get_table_names())
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert tables_first == tables_second
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_downgrade_only_drops_what_this_migration_added(bare_migration_app):
    app = bare_migration_app("downgrade_scan_events.db")
    with app.app_context():
        migrate_upgrade()
        tables_before = set(inspect(shared_db.engine).get_table_names())
        cols_before = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}

        migrate_downgrade(revision=PRIOR_HEAD)
        tables_after = set(inspect(shared_db.engine).get_table_names())
        cols_after = {c["name"] for c in inspect(shared_db.engine).get_columns("projects")}
    assert tables_before - tables_after == {"scan_events"}
    assert cols_before - cols_after == {"fallback_pair_id"}


def test_client_event_id_unique_index_rejects_duplicate_raw_insert(bare_migration_app):
    app = bare_migration_app("unique_client_event_id.db")
    with app.app_context():
        migrate_upgrade()
        shared_db.session.execute(text("INSERT INTO projects (name, is_active) VALUES ('p', 1)"))
        shared_db.session.commit()
        project_id = shared_db.session.execute(text("select id from projects")).fetchall()[0][0]

        shared_db.session.execute(text(
            "INSERT INTO scan_events (project_id, event_type, client_event_id) "
            f"VALUES ({project_id}, 'recognition_timeout', 'dup-token')"
        ))
        shared_db.session.commit()

        with pytest.raises(Exception):
            shared_db.session.execute(text(
                "INSERT INTO scan_events (project_id, event_type, client_event_id) "
                f"VALUES ({project_id}, 'camera_unavailable', 'dup-token')"
            ))
            shared_db.session.commit()
        shared_db.session.rollback()


def test_check_constraint_rejects_unknown_event_type(bare_migration_app):
    app = bare_migration_app("check_event_type.db")
    with app.app_context():
        migrate_upgrade()
        shared_db.session.execute(text("INSERT INTO projects (name, is_active) VALUES ('p', 1)"))
        shared_db.session.commit()
        project_id = shared_db.session.execute(text("select id from projects")).fetchall()[0][0]

        with pytest.raises(Exception):
            shared_db.session.execute(text(
                "INSERT INTO scan_events (project_id, event_type, client_event_id) "
                f"VALUES ({project_id}, 'matched_scan', 'bad-type-token')"
            ))
            shared_db.session.commit()
        shared_db.session.rollback()


def test_existing_scan_log_rows_survive_migration_unchanged(bare_migration_app):
    """Backward compatibility: a pre-migration ScanLog row (created against
    the prior head's schema, before scan_events/fallback_pair_id ever
    existed) must remain valid and keep meaning "matched scan"
    (is_successful=True) after upgrading to the new head - ScanLog's own
    schema is untouched by this migration."""
    app = bare_migration_app("scan_log_backward_compat.db")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        shared_db.session.execute(text(
            "INSERT INTO users (email, password_hash, is_verified) VALUES ('u@example.com', 'x', 1)"
        ))
        shared_db.session.commit()
        user_id = shared_db.session.execute(text("select id from users")).fetchall()[0][0]
        shared_db.session.execute(text(
            "INSERT INTO projects (name, owner_user_id, is_active) VALUES ('p', :uid, 1)"
        ), {"uid": user_id})
        shared_db.session.commit()
        project_id = shared_db.session.execute(text("select id from projects")).fetchall()[0][0]
        shared_db.session.execute(text(
            "INSERT INTO scan_logs (project_id, user_id, scan_session_id, is_successful, counted) "
            "VALUES (:pid, :uid, 'pre-migration-session', 1, 1)"
        ), {"pid": project_id, "uid": user_id})
        shared_db.session.commit()

        migrate_upgrade()  # to new head

        row = shared_db.session.execute(text(
            "SELECT is_successful, counted, scan_session_id FROM scan_logs WHERE project_id = :pid"
        ), {"pid": project_id}).fetchone()
        assert row is not None
        assert bool(row[0]) is True  # is_successful unchanged
        assert bool(row[1]) is True  # counted unchanged
        assert row[2] == "pre-migration-session"
