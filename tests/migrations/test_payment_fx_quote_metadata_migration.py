from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_REVISION = "e9b4d7a2c815"
FX_REVISION = "f6a8d0c2e4b9"
QUOTE_COLUMNS = {
    "base_amount",
    "base_currency",
    "quoted_amount",
    "quoted_currency",
    "fx_rate",
    "fx_rate_source",
    "fx_rate_timestamp",
}


def _migration_app(tmp_path, name):
    app = Flask(name)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)
    return app


def test_fx_revision_extends_current_content_report_head():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision(FX_REVISION)

    assert revision.down_revision == PRIOR_REVISION
    assert len(script.get_heads()) == 1
    assert script.get_current_head() == FX_REVISION


def test_upgrade_adds_nullable_quote_columns_to_payment_tables(tmp_path):
    app = _migration_app(tmp_path, "fx_quote_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        inspector = inspect(shared_db.engine)
        assert not (QUOTE_COLUMNS & {c["name"] for c in inspector.get_columns("payment_orders")})
        assert not (QUOTE_COLUMNS & {c["name"] for c in inspector.get_columns("addon_purchases")})

        migrate_upgrade(revision=FX_REVISION)
        inspector = inspect(shared_db.engine)
        order_columns = {c["name"]: c for c in inspector.get_columns("payment_orders")}
        purchase_columns = {c["name"]: c for c in inspector.get_columns("addon_purchases")}

    assert QUOTE_COLUMNS <= set(order_columns)
    assert QUOTE_COLUMNS <= set(purchase_columns)
    for column in QUOTE_COLUMNS:
        assert order_columns[column]["nullable"] is True
        assert purchase_columns[column]["nullable"] is True


def test_legacy_inr_rows_remain_valid_without_invented_fx_metadata(tmp_path):
    app = _migration_app(tmp_path, "fx_quote_legacy")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_REVISION)
        shared_db.session.execute(text(
            "INSERT INTO users (email, password_hash, subscription_status, subscribed_project_limit, subscribed_scan_limit) "
            "VALUES ('legacy@example.com', 'hash', 'trial', 1, 50)"
        ))
        shared_db.session.execute(text(
            "INSERT INTO subscription_plans (plan_name, plan_amount, total_project_limit, total_scan_limit, max_pairs_per_project) "
            "VALUES ('Legacy Plan', 999.0, 1, 50, 1)"
        ))
        user_id = shared_db.session.execute(text("SELECT id FROM users WHERE email='legacy@example.com'")).scalar()
        plan_id = shared_db.session.execute(text("SELECT id FROM subscription_plans WHERE plan_name='Legacy Plan'")).scalar()
        shared_db.session.execute(text(
            "INSERT INTO payment_orders "
            "(order_id, user_id, plan_id, amount, currency, total_amount, status) "
            "VALUES ('ORD_LEGACY_FX', :user_id, :plan_id, 999.0, 'INR', 999.0, 'success')"
        ), {"user_id": user_id, "plan_id": plan_id})
        shared_db.session.commit()

        migrate_upgrade(revision=FX_REVISION)
        row = shared_db.session.execute(text(
            "SELECT base_amount, base_currency, quoted_amount, quoted_currency, fx_rate, fx_rate_source "
            "FROM payment_orders WHERE order_id='ORD_LEGACY_FX'"
        )).one()

    assert tuple(row) == (None, None, None, None, None, None)


def test_downgrade_removes_only_quote_columns(tmp_path):
    app = _migration_app(tmp_path, "fx_quote_downgrade")
    with app.app_context():
        migrate_upgrade(revision=FX_REVISION)
        migrate_downgrade(revision=PRIOR_REVISION)
        inspector = inspect(shared_db.engine)

    assert not (QUOTE_COLUMNS & {c["name"] for c in inspector.get_columns("payment_orders")})
    assert not (QUOTE_COLUMNS & {c["name"] for c in inspector.get_columns("addon_purchases")})
