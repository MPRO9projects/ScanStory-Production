"""Migration c1a7f3d95e24: ownership history survives project delete (V1.1 P0-2)."""
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "a9d3c7e1b502"
HISTORY_REVISION = "c1a7f3d95e24"
TABLES = ("project_ownership_transfers", "project_ownership_claims")


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


def _seed_history(project_id=41):
    shared_db.session.execute(text(
        "INSERT INTO project_ownership_transfers "
        "(project_id, initiated_by_user_id, from_owner_user_id, to_user_id, retain_vendor_management, status) "
        "VALUES (:pid, 1, 1, 2, 0, 'COMPLETED')"
    ), {"pid": project_id})
    shared_db.session.execute(text(
        "INSERT INTO project_ownership_claims (project_id, claimant_user_id, status) "
        "VALUES (:pid, 2, 'REJECTED')"
    ), {"pid": project_id})
    shared_db.session.commit()


def _columns(table):
    return {c["name"]: c for c in inspect(shared_db.engine).get_columns(table)}


def test_revision_is_the_single_linear_head_on_top_of_wave4():
    script = _script_directory()
    assert script.get_revision(HISTORY_REVISION).down_revision == PRIOR_REVISION
    assert [rev.revision for rev in script.get_revisions("heads")] == [HISTORY_REVISION]


def test_upgrade_relaxes_project_id_adds_history_columns_and_keeps_rows(tmp_path):
    app = _migration_app(tmp_path, "ownership_history_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        for table in TABLES:
            assert _columns(table)["project_id"]["nullable"] is False
        _seed_history()

        migrate_upgrade(revision=HISTORY_REVISION)

        for table in TABLES:
            columns = _columns(table)
            assert columns["project_id"]["nullable"] is True
            assert "historical_project_id" in columns
            assert "historical_project_name" in columns
            index_names = {i["name"] for i in inspect(shared_db.engine).get_indexes(table)}
            assert f"ix_{table}_historical_project_id" in index_names

        # No data loss, and no invented backfill: live rows keep project_id and
        # read historical_* as NULL because they were never detached.
        transfer = shared_db.session.execute(text(
            "SELECT project_id, historical_project_id, historical_project_name, status "
            "FROM project_ownership_transfers"
        )).fetchall()
        claim = shared_db.session.execute(text(
            "SELECT project_id, historical_project_id, status FROM project_ownership_claims"
        )).fetchall()

    assert transfer == [(41, None, None, "COMPLETED")]
    assert claim == [(41, None, "REJECTED")]


def test_downgrade_is_clean_when_no_history_has_been_detached(tmp_path):
    app = _migration_app(tmp_path, "ownership_history_downgrade")
    with app.app_context():
        migrate_upgrade(revision=HISTORY_REVISION)
        _seed_history()

        migrate_downgrade(revision=PRIOR_REVISION)

        for table in TABLES:
            columns = _columns(table)
            assert columns["project_id"]["nullable"] is False
            assert "historical_project_id" not in columns
            assert "historical_project_name" not in columns
        rows = shared_db.session.execute(text("SELECT COUNT(*) FROM project_ownership_transfers")).scalar()
    assert rows == 1


def test_downgrade_refuses_to_destroy_detached_audit_rows(tmp_path):
    app = _migration_app(tmp_path, "ownership_history_downgrade_guard")
    with app.app_context():
        migrate_upgrade(revision=HISTORY_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO project_ownership_transfers "
            "(project_id, initiated_by_user_id, from_owner_user_id, to_user_id, retain_vendor_management, "
            "status, historical_project_id, historical_project_name) "
            "VALUES (NULL, 1, 1, 2, 0, 'COMPLETED', 77, 'Deleted Project')"
        ))
        shared_db.session.commit()

        # RuntimeError from the migration; flask_migrate's error wrapper turns it
        # into SystemExit(1) for the CLI. Either way the downgrade must abort.
        with pytest.raises((RuntimeError, SystemExit)):
            migrate_downgrade(revision=PRIOR_REVISION)

        # Aborted BEFORE dropping anything: the evidence and its columns remain.
        surviving = shared_db.session.execute(text(
            "SELECT historical_project_id, historical_project_name FROM project_ownership_transfers"
        )).fetchall()
        assert _columns("project_ownership_transfers")["project_id"]["nullable"] is True
        version = shared_db.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert surviving == [(77, "Deleted Project")]
    assert version == HISTORY_REVISION
