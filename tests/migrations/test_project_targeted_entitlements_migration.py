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
PRIOR_REVISION = "e5f6a7b8c9d0"
COMMERCIAL_REVISION = "a1c3e5b7d9f2"


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


def test_project_targeted_entitlements_revision_boundary():
    script = _script_directory()
    revision = script.get_revision(COMMERCIAL_REVISION)
    assert revision.down_revision == PRIOR_REVISION


def test_upgrade_adds_project_targeting_and_content_reports(tmp_path):
    app = _migration_app(tmp_path, "commercial_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        # A pre-existing account-level add-on purchase must survive with a
        # NULL project_id (account add-ons are never project-targeted).
        shared_db.session.execute(text(
            "INSERT INTO addon_catalog (code, name, addon_type, unit_amount, currency, is_active, is_commercially_available, created_at, updated_at) "
            "VALUES ('LEGACY_SCANS', 'Legacy Scans', 'EXTRA_SCANS', 99.0, 'INR', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        shared_db.session.execute(text(
            "INSERT INTO users (email, password_hash, created_at) VALUES ('legacy@example.com', 'x', CURRENT_TIMESTAMP)"
        ))
        shared_db.session.execute(text(
            "INSERT INTO addon_purchases (order_id, user_id, catalog_id, quantity, amount, total_amount, currency, status, created_at, updated_at) "
            "VALUES ('ADDON_LEGACY_1', 1, 1, 1, 99.0, 99.0, 'INR', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        shared_db.session.execute(text(
            "INSERT INTO entitlement_transactions (user_id, entitlement_type, delta_value, source_type, source_id, created_at) "
            "VALUES (1, 'EXTRA_SCANS', 100, 'addon_purchase', 1, CURRENT_TIMESTAMP)"
        ))
        shared_db.session.commit()

        migrate_upgrade(revision=COMMERCIAL_REVISION)

        inspector = inspect(shared_db.engine)
        purchase_cols = {c["name"] for c in inspector.get_columns("addon_purchases")}
        tx_cols = {c["name"] for c in inspector.get_columns("entitlement_transactions")}
        report_cols = {c["name"] for c in inspector.get_columns("content_reports")}
        backfilled = shared_db.session.execute(text(
            "SELECT project_id FROM addon_purchases WHERE order_id = 'ADDON_LEGACY_1'"
        )).fetchone()
        tx_backfilled = shared_db.session.execute(text(
            "SELECT project_id FROM entitlement_transactions WHERE source_id = 1"
        )).fetchone()

    assert "project_id" in purchase_cols
    assert "project_id" in tx_cols
    assert {
        "id", "project_id", "reporter_user_id", "reporter_email", "reporter_session_hash",
        "reporter_ip_hash", "reason", "details", "status", "created_at", "reviewed_at",
        "reviewed_by_admin_id", "resolution_action", "resolution_reason", "metadata_json",
    } <= report_cols
    # Default for existing account-level rows is NULL, not a fabricated target.
    assert backfilled[0] is None
    assert tx_backfilled[0] is None


def test_downgrade_removes_only_this_revisions_objects(tmp_path):
    app = _migration_app(tmp_path, "commercial_downgrade")
    with app.app_context():
        migrate_upgrade(revision=COMMERCIAL_REVISION)
        migrate_downgrade(revision=PRIOR_REVISION)

        inspector = inspect(shared_db.engine)
        tables = set(inspector.get_table_names())
        purchase_cols = {c["name"] for c in inspector.get_columns("addon_purchases")}
        tx_cols = {c["name"] for c in inspector.get_columns("entitlement_transactions")}

    assert "content_reports" not in tables
    assert "project_id" not in purchase_cols
    assert "project_id" not in tx_cols
    # Domain 2A objects are untouched by this downgrade.
    assert {"project_service_coverages", "project_ownership_transfers", "addon_purchases"} <= tables
