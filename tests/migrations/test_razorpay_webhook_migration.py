"""Migration checks for ebeab1cf4ec9 (razorpay webhook events), added by
v1/razorpay-webhook-reconciliation on top of the 54a108a17fa7 head.

Uses the same bare-Flask-app-bound-to-shared-db pattern as
tests/migrations/test_alembic_foundation.py's bare_migration_app fixture
(duplicated locally rather than imported, since that fixture is defined in
its own test module, not tests/conftest.py) - deliberately never imports
app.py, whose module-level `db.create_all()` would otherwise create every
table (including this one) before Alembic's own autogenerate/upgrade ever
runs, making "upgrade against a genuinely clean/old-head database" ambiguous
to test through the real app.
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
PRIOR_HEAD = "54a108a17fa7"


def _current_head_revision():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture()
def bare_migration_app(tmp_path):
    created = []

    def _make(name):
        app = Flask(f"webhook_migration_test_{name}")
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


def test_clean_database_upgrade_includes_webhook_events_table(tmp_path, bare_migration_app):
    app = bare_migration_app("clean_webhook.db")
    with app.app_context():
        migrate_upgrade()
        tables = set(inspect(shared_db.engine).get_table_names())
        assert "razorpay_webhook_events" in tables
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_upgrade_from_prior_head_reaches_new_head(bare_migration_app):
    app = bare_migration_app("from_prior_head.db")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        tables_before = set(inspect(shared_db.engine).get_table_names())
        assert "razorpay_webhook_events" not in tables_before

        migrate_upgrade()  # to new head
        tables_after = set(inspect(shared_db.engine).get_table_names())
        assert "razorpay_webhook_events" in tables_after
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_repeated_upgrade_is_idempotent(bare_migration_app):
    app = bare_migration_app("repeat_webhook.db")
    with app.app_context():
        migrate_upgrade()
        tables_first = set(inspect(shared_db.engine).get_table_names())
        migrate_upgrade()  # must be a no-op, not an error
        tables_second = set(inspect(shared_db.engine).get_table_names())
        version_rows = shared_db.session.execute(text("select version_num from alembic_version")).fetchall()
    assert tables_first == tables_second
    assert [row[0] for row in version_rows] == [_current_head_revision()]


def test_downgrade_only_drops_what_this_migration_added(bare_migration_app):
    app = bare_migration_app("downgrade_webhook.db")
    with app.app_context():
        migrate_upgrade()
        tables_before = set(inspect(shared_db.engine).get_table_names())
        migrate_downgrade(revision=PRIOR_HEAD)
        tables_after = set(inspect(shared_db.engine).get_table_names())
    removed = tables_before - tables_after
    assert removed == {"razorpay_webhook_events"}


def test_idempotency_key_unique_index_rejects_duplicate_raw_insert(bare_migration_app):
    app = bare_migration_app("unique_index_webhook.db")
    with app.app_context():
        migrate_upgrade()
        shared_db.session.execute(text(
            "INSERT INTO razorpay_webhook_events "
            "(idempotency_key, event_type, payload_hash, processing_status, attempt_count) "
            "VALUES ('raw-dup', 'payment.captured', 'a', 'received', 1)"
        ))
        shared_db.session.commit()

        with pytest.raises(Exception):
            shared_db.session.execute(text(
                "INSERT INTO razorpay_webhook_events "
                "(idempotency_key, event_type, payload_hash, processing_status, attempt_count) "
                "VALUES ('raw-dup', 'payment.captured', 'b', 'received', 1)"
            ))
            shared_db.session.commit()
        shared_db.session.rollback()
