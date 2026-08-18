"""Migration e9b4d7a2c815: ContentReport survives project delete (V1.1 production-ops).

WHAT IS BEING PROVEN
`content_reports.project_id` moves from NOT NULL + unnamed plain FK (PostgreSQL
NO ACTION) to nullable + `fk_content_reports_project_id_projects` with
ON DELETE SET NULL, so a hard-deleted project DETACHES its moderation reports
instead of destroying them.

ENGINE SELECTION - same contract as test_migrated_schema_lane.py, which is
reused deliberately rather than reinvented: set ``SCANSTORY_QA_DATABASE_URL`` to
a DISPOSABLE PostgreSQL database and the whole lane runs there, which is the
production-shaped run against the real constraint engine. With it unset the lane
runs on a temporary SQLite file with ``PRAGMA foreign_keys=ON``, because SQLite
ignores every foreign key otherwise - which is exactly how the sibling P0-5
defect stayed invisible through a green suite. The ON DELETE assertions below
are therefore meaningful on either backend; only the constraint engine differs.

NEVER point the QA URL at a production database: the lane drops and recreates
the schema.
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

PRIOR_REVISION = "c1a7f3d95e24"
REPORT_REVISION = "e9b4d7a2c815"
TABLE = "content_reports"
FK_NAME = "fk_content_reports_project_id_projects"

QA_URL_ENV = "SCANSTORY_QA_DATABASE_URL"

# Every index a1c3e5b7d9f2 created on content_reports. None of them may be lost
# by the rebuild - on SQLite the table is physically recreated, so this is a real
# risk rather than a formality.
EXPECTED_INDEXES = {
    "ix_content_reports_project_id",
    "ix_content_reports_reporter_user_id",
    "ix_content_reports_reporter_session_hash",
    "ix_content_reports_reporter_ip_hash",
    "ix_content_reports_reason",
    "ix_content_reports_status",
    "ix_content_reports_reviewed_by_admin_id",
    "ix_content_reports_project_status",
    "ix_content_reports_created_at",
}


def _qa_url():
    return (os.environ.get(QA_URL_ENV) or "").strip()


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


@pytest.fixture()
def migration_app(tmp_path):
    """Flask app bound to a disposable database with foreign keys ENFORCED."""
    app = Flask("content_report_delete_migration")
    qa_url = _qa_url()
    app.config["SQLALCHEMY_DATABASE_URI"] = qa_url or (
        f"sqlite:///{(tmp_path / 'content_reports.db').as_posix()}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)

    with app.app_context():
        engine = shared_db.engine
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine, "connect")
            def _enable_sqlite_fks(dbapi_connection, _record):  # pragma: no cover - hook
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            engine.dispose()
        else:
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _project(name):
    shared_db.session.execute(
        text("INSERT INTO projects (name, is_active) VALUES (:n, TRUE)"
             if shared_db.engine.dialect.name != "sqlite"
             else "INSERT INTO projects (name, is_active) VALUES (:n, 1)"),
        {"n": name},
    )
    shared_db.session.commit()
    return shared_db.session.execute(
        text("SELECT id FROM projects WHERE name = :n"), {"n": name}
    ).scalar()


def _admin(email="moderator@example.com"):
    shared_db.session.execute(
        text("INSERT INTO admins (email, password_hash) VALUES (:e, 'x')"), {"e": email}
    )
    shared_db.session.commit()
    return shared_db.session.execute(
        text("SELECT id FROM admins WHERE email = :e"), {"e": email}
    ).scalar()


def _report(project_id, **overrides):
    """Insert a fully populated report: every field a moderator would rely on."""
    values = {
        "project_id": project_id,
        "reporter_user_id": None,
        "reporter_email": "reporter@example.com",
        "reporter_session_hash": "session-hash-abc",
        "reporter_ip_hash": "ip-hash-def",
        "reason": "COPYRIGHT_OR_IP",
        "details": "Uses my footage without permission.",
        "status": "ACTION_TAKEN",
        "reviewed_by_admin_id": None,
        "resolution_action": "PROJECT_SUSPENDED",
        "resolution_reason": "Confirmed with the rights holder.",
        "metadata_json": '{"source":"web"}',
    }
    values.update(overrides)
    shared_db.session.execute(
        text(
            "INSERT INTO content_reports "
            "(project_id, reporter_user_id, reporter_email, reporter_session_hash, "
            " reporter_ip_hash, reason, details, status, reviewed_by_admin_id, "
            " resolution_action, resolution_reason, metadata_json) "
            "VALUES (:project_id, :reporter_user_id, :reporter_email, :reporter_session_hash, "
            " :reporter_ip_hash, :reason, :details, :status, :reviewed_by_admin_id, "
            " :resolution_action, :resolution_reason, :metadata_json)"
        ),
        values,
    )
    shared_db.session.commit()
    return shared_db.session.execute(text("SELECT MAX(id) FROM content_reports")).scalar()


def _project_id_column():
    return next(
        c for c in inspect(shared_db.engine).get_columns(TABLE) if c["name"] == "project_id"
    )


def _project_fk():
    for fk in inspect(shared_db.engine).get_foreign_keys(TABLE):
        if fk.get("constrained_columns") == ["project_id"]:
            return fk
    return None


def _delete_project(project_id):
    shared_db.session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": project_id})
    shared_db.session.commit()


# ---------------------------------------------------------------------------
# 1. revision graph
# ---------------------------------------------------------------------------
def test_revision_is_the_single_linear_head_on_top_of_the_ownership_history_fix():
    script = _script_directory()
    assert script.get_revision(REPORT_REVISION).down_revision == PRIOR_REVISION
    assert [rev.revision for rev in script.get_revisions("heads")] == [REPORT_REVISION]


# ---------------------------------------------------------------------------
# 2-6. what the upgrade does to the schema and to rows that already exist
# ---------------------------------------------------------------------------
def test_upgrade_preserves_every_existing_report_row(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        first = _project("Live One")
        second = _project("Live Two")
        _report(first)
        _report(second, reason="SPAM", status="OPEN")
        before = shared_db.session.execute(text("SELECT COUNT(*) FROM content_reports")).scalar()

        migrate_upgrade(revision=REPORT_REVISION)

        after = shared_db.session.execute(text("SELECT COUNT(*) FROM content_reports")).scalar()
    assert before == 2
    assert after == 2


def test_upgrade_leaves_existing_project_id_values_untouched(migration_app):
    """Only FUTURE deletions detach. A live reference is never nulled by the upgrade."""
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        first = _project("Keeper One")
        second = _project("Keeper Two")
        report_one = _report(first)
        report_two = _report(second, reason="SPAM")

        migrate_upgrade(revision=REPORT_REVISION)

        rows = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())
    assert rows == {report_one: first, report_two: second}


def test_upgrade_makes_project_id_nullable(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        assert _project_id_column()["nullable"] is False

        migrate_upgrade(revision=REPORT_REVISION)

        assert _project_id_column()["nullable"] is True


def test_upgrade_declares_the_project_foreign_key_with_on_delete_set_null(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        before = _project_fk()
        assert (before.get("options") or {}).get("ondelete") in (None, "")

        migrate_upgrade(revision=REPORT_REVISION)

        after = _project_fk()
    assert after is not None, "project_id foreign key missing after upgrade"
    assert after["name"] == FK_NAME, "constraint must carry the repo's explicit name"
    assert after["referred_table"] == "projects"
    assert (after.get("options") or {}).get("ondelete", "").upper() == "SET NULL"


def test_upgrade_retains_every_existing_index(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        names = {i["name"] for i in inspect(shared_db.engine).get_indexes(TABLE)}
    assert EXPECTED_INDEXES <= names, f"lost indexes: {EXPECTED_INDEXES - names}"


# ---------------------------------------------------------------------------
# 7-11. the actual point: what a project delete now does at the DATABASE level
# ---------------------------------------------------------------------------
def test_deleting_the_reported_project_sets_project_id_null(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        project_id = _project("Doomed")
        report_id = _report(project_id)

        _delete_project(project_id)

        detached = shared_db.session.execute(
            text("SELECT project_id FROM content_reports WHERE id = :i"), {"i": report_id}
        ).scalar()
    assert detached is None


def test_deleting_the_reported_project_does_not_delete_the_report(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        project_id = _project("Doomed")
        report_id = _report(project_id)

        _delete_project(project_id)

        surviving = shared_db.session.execute(
            text("SELECT COUNT(*) FROM content_reports WHERE id = :i"), {"i": report_id}
        ).scalar()
        project_gone = shared_db.session.execute(
            text("SELECT COUNT(*) FROM projects WHERE id = :i"), {"i": project_id}
        ).scalar()
    assert surviving == 1, "the moderation record must outlive the project"
    assert project_gone == 0, "the project itself must still be deleted"


def test_deleting_one_project_leaves_reports_on_other_projects_alone(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        doomed = _project("Doomed")
        survivor = _project("Survivor")
        doomed_report = _report(doomed)
        other_report = _report(survivor, reason="SPAM", status="OPEN")

        _delete_project(doomed)

        rows = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())
    assert rows == {doomed_report: None, other_report: survivor}


def test_detached_report_keeps_all_moderation_metadata(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        admin_id = _admin()
        project_id = _project("Doomed")
        report_id = _report(project_id, reviewed_by_admin_id=admin_id)
        shared_db.session.execute(
            text("UPDATE content_reports SET reviewed_at = created_at WHERE id = :i"),
            {"i": report_id},
        )
        shared_db.session.commit()

        _delete_project(project_id)

        row = shared_db.session.execute(text(
            "SELECT reason, details, status, resolution_action, resolution_reason, "
            "metadata_json, reviewed_by_admin_id, reviewed_at IS NOT NULL, "
            "created_at IS NOT NULL FROM content_reports WHERE id = :i"
        ), {"i": report_id}).fetchone()
    assert row == (
        "COPYRIGHT_OR_IP",
        "Uses my footage without permission.",
        "ACTION_TAKEN",
        "PROJECT_SUSPENDED",
        "Confirmed with the rights holder.",
        '{"source":"web"}',
        admin_id,
        True,
        True,
    )


def test_detached_report_keeps_all_reporter_metadata(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        project_id = _project("Doomed")
        report_id = _report(project_id)

        _delete_project(project_id)

        row = shared_db.session.execute(text(
            "SELECT reporter_email, reporter_session_hash, reporter_ip_hash, reporter_user_id "
            "FROM content_reports WHERE id = :i"
        ), {"i": report_id}).fetchone()
    assert row == ("reporter@example.com", "session-hash-abc", "ip-hash-def", None)


# ---------------------------------------------------------------------------
# 12-13. upgrade shape independent of the data present
# ---------------------------------------------------------------------------
def test_upgrade_works_on_an_empty_content_reports_table(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        assert shared_db.session.execute(text("SELECT COUNT(*) FROM content_reports")).scalar() == 0

        migrate_upgrade(revision=REPORT_REVISION)

        assert _project_id_column()["nullable"] is True
        assert (_project_fk().get("options") or {}).get("ondelete", "").upper() == "SET NULL"
        assert shared_db.session.execute(text("SELECT COUNT(*) FROM content_reports")).scalar() == 0


def test_upgrade_handles_many_projects_each_with_several_reports(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        expected = {}
        for index in range(3):
            project_id = _project(f"Bulk {index}")
            for reason in ("SPAM", "COPYRIGHT_OR_IP", "HATE_OR_HARASSMENT"):
                expected[_report(project_id, reason=reason)] = project_id

        migrate_upgrade(revision=REPORT_REVISION)

        rows = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())
        # And the new rule holds for all of them, not just the first.
        doomed = sorted(set(expected.values()))[0]
        _delete_project(doomed)
        after = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())

    assert len(expected) == 9
    assert rows == expected
    assert len(after) == 9, "no report may be destroyed by the delete"
    assert {report_id for report_id, pid in after.items() if pid is None} == {
        report_id for report_id, pid in expected.items() if pid == doomed
    }


# ---------------------------------------------------------------------------
# 14-16. downgrade policy: preserve evidence over a convenient rollback
# ---------------------------------------------------------------------------
def test_downgrade_is_clean_when_no_report_has_been_detached(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        project_id = _project("Still Here")
        _report(project_id)

        migrate_downgrade(revision=PRIOR_REVISION)

        assert _project_id_column()["nullable"] is False
        assert (_project_fk().get("options") or {}).get("ondelete") in (None, "")
        rows = shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports")
        ).fetchall()
        indexes = {i["name"] for i in inspect(shared_db.engine).get_indexes(TABLE)}
    assert [row[1] for row in rows] == [project_id]
    assert EXPECTED_INDEXES <= indexes


def test_downgrade_refuses_rather_than_destroy_detached_reports(migration_app):
    """Policy (A): refuse. Deleting moderation history to make a rollback succeed
    is the very data loss this migration exists to prevent."""
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        doomed = _project("Doomed")
        survivor = _project("Survivor")
        detached_report = _report(doomed)
        live_report = _report(survivor, reason="SPAM")
        _delete_project(doomed)

        # RuntimeError from the migration; flask_migrate's error wrapper turns it
        # into SystemExit(1) for the CLI. Either way the downgrade must abort.
        with pytest.raises((RuntimeError, SystemExit)):
            migrate_downgrade(revision=PRIOR_REVISION)

        # Aborted BEFORE touching anything: both rows and the relaxed column stay.
        rows = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())
        assert _project_id_column()["nullable"] is True
        version = shared_db.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert rows == {detached_report: None, live_report: survivor}
    assert version == REPORT_REVISION, "a refused downgrade must not advance the version"


def test_upgrade_downgrade_upgrade_round_trip_destroys_no_report(migration_app):
    with migration_app.app_context():
        migrate_upgrade(revision=REPORT_REVISION)
        project_id = _project("Round Trip")
        report_id = _report(project_id)

        migrate_downgrade(revision=PRIOR_REVISION)
        migrate_upgrade(revision=REPORT_REVISION)

        rows = dict(shared_db.session.execute(
            text("SELECT id, project_id FROM content_reports ORDER BY id")
        ).fetchall())
        detail = shared_db.session.execute(text(
            "SELECT reason, status, resolution_reason FROM content_reports WHERE id = :i"
        ), {"i": report_id}).fetchone()
        # And the SET NULL rule is back after the round trip, not silently lost.
        assert (_project_fk().get("options") or {}).get("ondelete", "").upper() == "SET NULL"
    assert rows == {report_id: project_id}
    assert detail == ("COPYRIGHT_OR_IP", "ACTION_TAKEN", "Confirmed with the rights holder.")
