"""Migrated-schema test lane (Wave 1).

The systemic finding behind P0-2 and P0-5 is that the whole suite builds its
schema with ``db.create_all()`` on SQLite, so **the schema under test is not the
schema that ships**. A CHECK constraint that exists only in the migration, and a
foreign key that only PostgreSQL enforces, are both invisible that way.

This lane is ADDITIVE - it does not replace the fast SQLite suite. It runs
``alembic upgrade head`` against a disposable database and then asserts against
the *migrated* schema.

ENGINE SELECTION
    Set ``SCANSTORY_QA_DATABASE_URL`` to a disposable PostgreSQL database and
    this lane runs there, which is the production-shaped run and the one that
    exercises real foreign-key enforcement and ``SELECT ... FOR UPDATE``.
    With it unset the lane still runs, on a temporary SQLite file with
    ``PRAGMA foreign_keys=ON`` so foreign keys are genuinely enforced rather
    than silently ignored. Tests that can only be meaningful on PostgreSQL are
    skipped, and say so, rather than passing vacuously.

    NEVER point this at a production database: the lane creates and drops
    schema objects.
"""
import os
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import event, inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")

ADDON_CHECK_REVISION = "c3f7a1d5e9b4"
UPLOAD_FK_REVISION = "d4e8b2c6a0f3"
PRE_WAVE1_HEAD = "b2c4d6e8f0a1"

QA_URL_ENV = "SCANSTORY_QA_DATABASE_URL"


def _qa_url():
    return (os.environ.get(QA_URL_ENV) or "").strip()


def _is_postgres():
    return _qa_url().startswith(("postgresql://", "postgresql+"))


requires_postgres = pytest.mark.skipif(
    not _is_postgres(),
    reason=(
        "PostgreSQL-only behaviour. Set %s to a DISPOSABLE PostgreSQL database "
        "to run this (never a production one)." % QA_URL_ENV
    ),
)


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


@pytest.fixture()
def migrated_app(tmp_path):
    """Flask app bound to a disposable migrated database (PostgreSQL or SQLite)."""
    app = Flask("migrated_schema_lane")
    qa_url = _qa_url()
    app.config["SQLALCHEMY_DATABASE_URI"] = qa_url or (
        f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-lane-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)

    with app.app_context():
        engine = shared_db.engine
        if engine.dialect.name == "sqlite":
            # Without this SQLite ignores every FK, which is precisely how P0-5
            # stayed invisible. Turn enforcement ON so the lane is meaningful.
            @event.listens_for(engine, "connect")
            def _enable_sqlite_fks(dbapi_connection, _record):  # pragma: no cover - hook
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            engine.dispose()
        else:
            # Disposable QA database: start from a known-empty schema.
            shared_db.session.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            shared_db.session.execute(text("CREATE SCHEMA public"))
            shared_db.session.commit()

        yield app

        try:
            shared_db.session.remove()
        finally:
            if shared_db.engine.dialect.name != "sqlite":
                shared_db.session.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                shared_db.session.execute(text("CREATE SCHEMA public"))
                shared_db.session.commit()
                shared_db.session.remove()
            shared_db.engine.dispose()


def _exec(sql, **params):
    return shared_db.session.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------
def test_wave1_revisions_keep_a_single_linear_head():
    # UPLOAD_FK_REVISION is no longer the head: Wave 2 added e7a3f9c2b1d5 on
    # top of it. Same treatment the refunds revision got when Wave 1 superseded
    # it - what must stay true is that the chain is single-headed and that the
    # Wave 1 revisions keep their exact place in it.
    script = _script_directory()
    assert len(script.get_heads()) == 1
    assert script.get_revision(ADDON_CHECK_REVISION).down_revision == PRE_WAVE1_HEAD
    assert script.get_revision(UPLOAD_FK_REVISION).down_revision == ADDON_CHECK_REVISION
    ancestry = {r.revision for r in script.iterate_revisions(script.get_current_head(), "base")}
    assert {ADDON_CHECK_REVISION, UPLOAD_FK_REVISION} <= ancestry


def test_historical_migrations_were_not_edited():
    original = Path(MIGRATIONS_DIR, "versions", "f4a8c2b91d70_addon_entitlement_foundation.py")
    body = original.read_text(encoding="utf-8")
    assert "'EXTRA_SCANS', 'VALIDITY_EXTENSION', 'PROJECT_CAPACITY')" in body
    assert "PROJECT_SERVICE_COVERAGE" not in body


# ---------------------------------------------------------------------------
# P0-2 against the migrated schema
# ---------------------------------------------------------------------------
def test_fresh_database_upgrades_to_head(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade()
        tables = set(inspect(shared_db.engine).get_table_names())
    assert {"addon_catalog", "upload_sessions", "projects", "payment_refunds"} <= tables


def test_upgrade_from_previous_head_permits_project_service_coverage(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade(revision=PRE_WAVE1_HEAD)
        migrate_upgrade(revision=ADDON_CHECK_REVISION)

        _exec(
            "INSERT INTO addon_catalog "
            "(code, name, addon_type, unit_amount, currency, validity_days_delta, "
            " is_active, is_commercially_available) "
            "VALUES (:code, :name, 'PROJECT_SERVICE_COVERAGE', 999.0, 'INR', 365, "
            "        :active, :available)",
            code="coverage-1y", name="Coverage 1y", active=True, available=True,
        )
        shared_db.session.commit()

        count = _exec(
            "SELECT COUNT(*) FROM addon_catalog WHERE addon_type = 'PROJECT_SERVICE_COVERAGE'"
        ).scalar()
    assert count == 1


def test_migrated_schema_still_rejects_an_invalid_addon_type(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade()
        with pytest.raises(Exception):
            _exec(
                "INSERT INTO addon_catalog "
                "(code, name, addon_type, unit_amount, currency, is_active, is_commercially_available) "
                "VALUES ('bad', 'bad', 'NOT_A_REAL_ADDON', 1.0, 'INR', :active, :available)",
                active=True, available=True,
            )
            shared_db.session.commit()
        shared_db.session.rollback()


def test_existing_rows_survive_the_constraint_replacement(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade(revision=PRE_WAVE1_HEAD)
        _exec(
            "INSERT INTO addon_catalog "
            "(code, name, addon_type, unit_amount, currency, scan_delta, is_active, is_commercially_available) "
            "VALUES ('legacy-scans', 'Legacy', 'EXTRA_SCANS', 100.0, 'INR', 100, :active, :available)",
            active=True, available=True,
        )
        shared_db.session.commit()

        migrate_upgrade(revision=ADDON_CHECK_REVISION)

        surviving = _exec("SELECT name FROM addon_catalog WHERE code = 'legacy-scans'").scalar()
    assert surviving == "Legacy"


def test_addon_check_downgrade_restores_the_narrower_constraint(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade(revision=ADDON_CHECK_REVISION)
        migrate_downgrade(revision=PRE_WAVE1_HEAD)

        with pytest.raises(Exception):
            _exec(
                "INSERT INTO addon_catalog "
                "(code, name, addon_type, unit_amount, currency, validity_days_delta, "
                " is_active, is_commercially_available) "
                "VALUES ('c', 'c', 'PROJECT_SERVICE_COVERAGE', 1.0, 'INR', 30, :active, :available)",
                active=True, available=True,
            )
            shared_db.session.commit()
        shared_db.session.rollback()


# ---------------------------------------------------------------------------
# P0-5 against the migrated schema, with foreign keys actually enforced
# ---------------------------------------------------------------------------
def _seed_project_with_upload_session():
    _exec(
        "INSERT INTO projects (id, name, is_active) VALUES (1, 'P', :active)",
        active=True,
    )
    _exec(
        "INSERT INTO project_pairs (id, project_id, pair_index, video_filename) "
        "VALUES (1, 1, 1, '1_1.mp4')"
    )
    _exec(
        "INSERT INTO upload_sessions "
        "(id, purpose, image_size, video_size, expected_total_size, current_offset, "
        " status, storage_token, project_id, pair_id, expires_at) "
        "VALUES (1, 'project_pair', 4, 6, 10, 10, 'completed', 'tok-1', 1, 1, :expires)",
        expires="2030-01-01 00:00:00",
    )
    shared_db.session.commit()


def test_upload_session_fk_declares_set_null_after_migration(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade()
        fks = inspect(shared_db.engine).get_foreign_keys("upload_sessions")
        by_column = {
            fk["constrained_columns"][0]: fk
            for fk in fks
            if fk.get("constrained_columns")
        }

    for column in ("project_id", "pair_id"):
        assert column in by_column, f"{column} foreign key missing after upgrade"
        options = by_column[column].get("options") or {}
        assert (options.get("ondelete") or "").upper() == "SET NULL"


def test_deleting_a_project_with_an_upload_session_does_not_raise(migrated_app):
    """The production-only failure: PostgreSQL raised IntegrityError here."""
    with migrated_app.app_context():
        migrate_upgrade()
        _seed_project_with_upload_session()

        _exec("DELETE FROM project_pairs WHERE project_id = 1")
        _exec("DELETE FROM projects WHERE id = 1")
        shared_db.session.commit()

        row = _exec(
            "SELECT project_id, pair_id FROM upload_sessions WHERE id = 1"
        ).first()

    assert row is not None, "upload session history must be retained"
    assert row[0] is None and row[1] is None, "references must be nulled, not dangling"


def test_upload_fk_downgrade_runs(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade()
        migrate_downgrade(revision=ADDON_CHECK_REVISION)
        fks = inspect(shared_db.engine).get_foreign_keys("upload_sessions")
    columns = {fk["constrained_columns"][0] for fk in fks if fk.get("constrained_columns")}
    assert {"project_id", "pair_id"} <= columns


# ---------------------------------------------------------------------------
# PostgreSQL-only behaviour the SQLite suite structurally cannot cover
# ---------------------------------------------------------------------------
@requires_postgres
def test_postgres_enforces_the_foreign_key_without_the_cascade(migrated_app):
    """Proves the lane would have caught P0-5: without SET NULL this errors."""
    with migrated_app.app_context():
        migrate_upgrade()
        _seed_project_with_upload_session()
        _exec("ALTER TABLE upload_sessions DROP CONSTRAINT fk_upload_sessions_project_id_projects")
        _exec(
            "ALTER TABLE upload_sessions ADD CONSTRAINT fk_tmp_no_action "
            "FOREIGN KEY (project_id) REFERENCES projects (id)"
        )
        shared_db.session.commit()

        with pytest.raises(Exception):
            _exec("DELETE FROM projects WHERE id = 1")
            shared_db.session.commit()
        shared_db.session.rollback()


@requires_postgres
def test_postgres_supports_select_for_update_on_the_pair_quota_path(migrated_app):
    """`SELECT ... FOR UPDATE` is skipped on SQLite, so it has never executed in CI."""
    with migrated_app.app_context():
        migrate_upgrade()
        _exec(
            "INSERT INTO projects (id, name, is_active) VALUES (2, 'Lockable', :active)",
            active=True,
        )
        shared_db.session.commit()

        locked = _exec("SELECT id FROM projects WHERE id = 2 FOR UPDATE").scalar()
        shared_db.session.commit()
    assert locked == 2


@requires_postgres
def test_postgres_check_constraint_is_present_by_name(migrated_app):
    with migrated_app.app_context():
        migrate_upgrade()
        names = {
            row[0]
            for row in _exec(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'addon_catalog'::regclass AND contype = 'c'"
            ).fetchall()
        }
    assert "ck_addon_catalog_type" in names
