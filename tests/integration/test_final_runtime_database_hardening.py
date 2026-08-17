import importlib
import sys
from pathlib import Path

import pytest

from scripts.migration import sqlite_to_postgresql_rehearsal as rehearsal


def _import_app_with_env(monkeypatch, **overrides):
    base = {
        "SCANSTORY_TESTING": "0",
        "SCANSTORY_PRODUCTION": "1",
        "FLASK_ENV": "production",
        "FLASK_SECRET_KEY": "production-test-secret",
        "DATABASE_URL": "postgresql+psycopg://user:pw@localhost:5432/scanstory_prod",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "mailer",
        "SMTP_PASS": "mailer-password",
        "MAIL_FROM": "no-reply@example.com",
        "SESSION_COOKIE_SECURE": "1",
        "SCANSTORY_DEV_TESTING": "0",
        "SCANSTORY_QUEUE_MODE": "rq",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "SCANSTORY_SKIP_STARTUP_BOOTSTRAP": "1",
        "RAZORPAY_KEY_ID": "rzp_test_key_id",
        "RAZORPAY_KEY_SECRET": "rzp_test_key_secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
    }
    base.update(overrides)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    sys.modules.pop("app", None)
    try:
        return importlib.import_module("app")
    finally:
        sys.modules.pop("app", None)


def test_production_rejects_fake_queue_mode(monkeypatch):
    with pytest.raises(RuntimeError, match="SCANSTORY_QUEUE_MODE=rq"):
        _import_app_with_env(monkeypatch, SCANSTORY_QUEUE_MODE="fake")


def test_production_requires_redis_for_rq(monkeypatch):
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        _import_app_with_env(monkeypatch, REDIS_URL=None)


def test_production_rejects_sqlite_database(monkeypatch):
    with pytest.raises(RuntimeError, match="DATABASE_URL=postgresql"):
        _import_app_with_env(monkeypatch, DATABASE_URL="sqlite:///instance/prod.db")


def test_development_runtime_rejects_sqlite_database(monkeypatch):
    with pytest.raises(RuntimeError, match="DATABASE_URL=postgresql"):
        _import_app_with_env(
            monkeypatch,
            SCANSTORY_PRODUCTION="0",
            FLASK_ENV="development",
            DATABASE_URL="sqlite:///instance/dev.db",
        )


def test_production_accepts_postgresql_and_rq(monkeypatch):
    app_module = _import_app_with_env(monkeypatch)
    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql")
    assert app_module.queue_mode() == "rq"


def test_runtime_startup_bootstrap_create_all_is_test_only(monkeypatch):
    with pytest.raises(RuntimeError, match="Runtime db.create_all\\(\\) bootstrap is disabled outside tests"):
        _import_app_with_env(
            monkeypatch,
            SCANSTORY_PRODUCTION="0",
            FLASK_ENV="development",
            SCANSTORY_SKIP_STARTUP_BOOTSTRAP="0",
        )


def test_development_testing_sqlite_and_fake_queue_remain_supported(monkeypatch, tmp_path):
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("FLASK_SECRET_KEY", "dev-test-secret")
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("SCANSTORY_SKIP_STARTUP_BOOTSTRAP", "1")
    sys.modules.pop("app", None)
    try:
        app_module = importlib.import_module("app")
        assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")
        assert app_module.queue_mode() == "fake"
    finally:
        sys.modules.pop("app", None)


def test_rehearsal_rejects_non_sqlite_source():
    with pytest.raises(SystemExit):
        rehearsal.validate_urls("postgresql+psycopg://user:pw@localhost/source", "postgresql+psycopg://user:pw@localhost/dest")


def test_rehearsal_rejects_non_postgres_destination():
    with pytest.raises(SystemExit):
        rehearsal.validate_urls("sqlite:///source.db", "sqlite:///dest.db")


def test_rehearsal_safe_url_label_hides_password():
    label = rehearsal.safe_url_label("postgresql+psycopg://user:secret@localhost:5432/scanstory_dev")
    assert "secret" not in label
    assert "localhost" in label
    assert "scanstory_dev" in label


def test_rehearsal_has_policy_review_and_sequence_reset_design():
    source = Path("scripts/migration/sqlite_to_postgresql_rehearsal.py").read_text(encoding="utf-8")
    assert "POLICY_REVIEW_TABLES" in source
    assert "otp_codes" in source
    assert "metadata = db.metadata" in source
    assert "metadata.sorted_tables" in source
    assert "reset_sequences" in source
    assert "pg_get_serial_sequence" in source
    assert "media_files_copied" in source
    assert "db.create_all" not in source


def test_env_example_documents_runtime_and_testing_contract():
    env = Path(".env.example").read_text(encoding="utf-8")
    for key in (
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "REDIS_URL",
        "SCANSTORY_QUEUE_MODE",
        "RQ_QUEUE_NAME",
        "RQ_DEFAULT_TIMEOUT",
        "SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES",
        "FLASK_SECRET_KEY",
        "SCANSTORY_DEV_TESTING",
        "SCANSTORY_TESTING",
    ):
        assert key in env
    assert "must remain 0 in production" in env
    assert "SQLite is supported only for isolated tests" in env
    assert "DATABASE_URL=postgresql+psycopg://" in env
    assert "Production startup rejects fake/inline" in env
    assert "fake" in env and "inline" in env and "rq" in env


def test_dev_scripts_are_non_destructive_and_avoid_migrate_generation():
    scripts = list(Path("scripts/dev").glob("*.ps1"))
    assert scripts
    combined = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    assert "flask --app app db upgrade" in combined
    assert "flask --app app db current" in combined
    assert "flask --app app db heads" in combined
    assert "seed-dev-test-users" in combined
    assert "flask db migrate" not in combined
    assert "db.create_all" not in combined
    assert "DROP " not in combined.upper()
    assert "TRUNCATE " not in combined.upper()
