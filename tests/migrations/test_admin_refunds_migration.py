from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "a1c3e5b7d9f2"
REFUND_REVISION = "b2c4d6e8f0a1"


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


def test_admin_refunds_migration_is_current_head():
    script = _script_directory()
    revision = script.get_revision(REFUND_REVISION)
    assert script.get_current_head() == REFUND_REVISION
    assert revision.down_revision == PRIOR_REVISION


def test_admin_refunds_upgrade_adds_refund_schema(tmp_path):
    app = _migration_app(tmp_path, "admin_refunds_upgrade")
    with app.app_context():
        migrate_upgrade(revision=REFUND_REVISION)
        inspector = inspect(shared_db.engine)
        refund_cols = {c["name"] for c in inspector.get_columns("payment_refunds")}
        webhook_cols = {c["name"] for c in inspector.get_columns("razorpay_webhook_events")}
        coverage_cols = {c["name"] for c in inspector.get_columns("project_service_coverages")}
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints("payment_refunds")
        }

    assert {
        "payment_order_id",
        "addon_purchase_id",
        "provider_refund_id",
        "status",
        "reconciliation_status",
        "idempotency_key",
    } <= refund_cols
    assert "payment_refund_id" in webhook_cols
    assert {"revoked_at", "revoked_by_refund_id"} <= coverage_cols
    assert {
        "uq_payment_refunds_payment_order_id",
        "uq_payment_refunds_addon_purchase_id",
        "uq_payment_refunds_provider_refund_id",
        "uq_payment_refunds_idempotency_key",
    } <= unique_names


def test_admin_refunds_downgrade_drops_only_refund_schema(tmp_path):
    app = _migration_app(tmp_path, "admin_refunds_downgrade")
    with app.app_context():
        migrate_upgrade(revision=REFUND_REVISION)
        migrate_downgrade(revision=PRIOR_REVISION)
        inspector = inspect(shared_db.engine)
        tables = set(inspector.get_table_names())
        webhook_cols = {c["name"] for c in inspector.get_columns("razorpay_webhook_events")}
        coverage_cols = {c["name"] for c in inspector.get_columns("project_service_coverages")}

    assert "payment_refunds" not in tables
    assert "payment_refund_id" not in webhook_cols
    assert {"revoked_at", "revoked_by_refund_id"}.isdisjoint(coverage_cols)
    assert "content_reports" in tables
