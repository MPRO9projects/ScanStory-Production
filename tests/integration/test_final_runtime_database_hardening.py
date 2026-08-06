from pathlib import Path

import pytest

from scripts.migration import sqlite_to_postgresql_rehearsal as rehearsal


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
        "FLASK_SECRET_KEY",
        "SCANSTORY_DEV_TESTING",
        "SCANSTORY_TESTING",
    ):
        assert key in env
    assert "must remain 0 in production" in env
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
