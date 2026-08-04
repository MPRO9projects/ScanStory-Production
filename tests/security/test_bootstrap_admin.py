"""bootstrap_database() administrator-creation hardening.

Previous behavior: on every app import (including the module-level
`with app.app_context(): ...` bootstrap block that runs unconditionally,
not just under `python app.py`), if zero Admin rows existed, an admin was
auto-created with BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD env vars
that defaulted to the hard-coded public values "admin@scanstory.com" /
"Admin@123" when unset - a standing default-credential backdoor on any
fresh production database.

Corrected behavior: bootstrap admin creation requires explicit opt-in via
BOOTSTRAP_ADMIN_ENABLED=1, with no default email/password. See
_resolve_bootstrap_admin_credentials / _maybe_create_bootstrap_admin.
"""
import importlib
import re
import sys
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.security


def _reimport_app_with_env(monkeypatch, tmp_path, bootstrap_env):
    """Fresh app import with a fully controlled bootstrap-admin env.

    bootstrap_env values of None delete the var; otherwise it's set.
    """
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    monkeypatch.setenv("SCANSTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCANSTORY_ADMIN_DATA_DIR", str(tmp_path / "data_admin"))
    monkeypatch.setenv("SCANSTORY_STATIC_UPLOADS_DIR", str(tmp_path / "static_uploads"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "bootstrap-admin-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    for key, value in bootstrap_env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)

    return importlib.import_module("app")


def _pop_app_module():
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)


# ---------------------------------------------------------------------------
# 4. Bootstrap disabled by default (item 4 of the task)
# ---------------------------------------------------------------------------

def test_bootstrap_disabled_by_default_creates_no_admin(monkeypatch, tmp_path):
    try:
        app_module = _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": None,
            "BOOTSTRAP_ADMIN_EMAIL": None,
            "BOOTSTRAP_ADMIN_PASSWORD": None,
        })
        with app_module.app.app_context():
            assert app_module.Admin.query.count() == 0
    finally:
        _pop_app_module()


def test_bootstrap_disabled_does_not_fail_startup(monkeypatch, tmp_path):
    # Import itself succeeding (no exception) is the assertion.
    try:
        _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": None,
            "BOOTSTRAP_ADMIN_EMAIL": None,
            "BOOTSTRAP_ADMIN_PASSWORD": None,
        })
    finally:
        _pop_app_module()


def test_old_default_credential_email_never_created_when_disabled(monkeypatch, tmp_path):
    try:
        app_module = _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": None,
        })
        with app_module.app.app_context():
            assert app_module.Admin.query.filter_by(email="admin@scanstory.com").first() is None
    finally:
        _pop_app_module()


# ---------------------------------------------------------------------------
# 5. Explicit enable: requires both values, validates length, never overwrites
# ---------------------------------------------------------------------------

def test_enabled_without_email_or_password_fails_clearly(monkeypatch, tmp_path):
    try:
        with pytest.raises(RuntimeError, match="BOOTSTRAP_ADMIN_EMAIL"):
            _reimport_app_with_env(monkeypatch, tmp_path, {
                "BOOTSTRAP_ADMIN_ENABLED": "1",
                "BOOTSTRAP_ADMIN_EMAIL": None,
                "BOOTSTRAP_ADMIN_PASSWORD": None,
            })
    finally:
        _pop_app_module()


def test_enabled_with_short_password_fails_clearly(monkeypatch, tmp_path):
    try:
        with pytest.raises(RuntimeError, match="too short"):
            _reimport_app_with_env(monkeypatch, tmp_path, {
                "BOOTSTRAP_ADMIN_ENABLED": "1",
                "BOOTSTRAP_ADMIN_EMAIL": "owner@example.com",
                "BOOTSTRAP_ADMIN_PASSWORD": "short1",
            })
    finally:
        _pop_app_module()


def test_enabled_with_valid_credentials_creates_admin(monkeypatch, tmp_path):
    try:
        app_module = _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": "1",
            "BOOTSTRAP_ADMIN_EMAIL": "Owner@Example.com",
            "BOOTSTRAP_ADMIN_PASSWORD": "a-real-operator-password",
        })
        with app_module.app.app_context():
            admin = app_module.Admin.query.filter_by(email="owner@example.com").first()
            assert admin is not None
            assert admin.role == "superadmin"
            assert admin.is_active is True
            assert check_password_hash(admin.password_hash, "a-real-operator-password")
            # The old hard-coded default password must not also work.
            assert not check_password_hash(admin.password_hash, "Admin@123")
    finally:
        _pop_app_module()


def test_enabled_does_not_overwrite_existing_admin(monkeypatch, tmp_path):
    try:
        app_module = _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": "1",
            "BOOTSTRAP_ADMIN_EMAIL": "first-owner@example.com",
            "BOOTSTRAP_ADMIN_PASSWORD": "first-real-password",
        })
        with app_module.app.app_context():
            assert app_module.Admin.query.count() == 1
            # Re-running bootstrap with different credentials must not add
            # a second admin or touch the existing one.
            import os
            os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "second-owner@example.com"
            os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "second-real-password"
            app_module._maybe_create_bootstrap_admin()
            app_module.db.session.commit()
            assert app_module.Admin.query.count() == 1
            assert app_module.Admin.query.filter_by(email="first-owner@example.com").first() is not None
            assert app_module.Admin.query.filter_by(email="second-owner@example.com").first() is None
    finally:
        _pop_app_module()


def test_password_never_printed_on_bootstrap(monkeypatch, tmp_path, capsys):
    secret_password = "totally-unique-marker-password-987654"
    try:
        _reimport_app_with_env(monkeypatch, tmp_path, {
            "BOOTSTRAP_ADMIN_ENABLED": "1",
            "BOOTSTRAP_ADMIN_EMAIL": "owner@example.com",
            "BOOTSTRAP_ADMIN_PASSWORD": secret_password,
        })
        captured = capsys.readouterr()
        assert secret_password not in captured.out
        assert secret_password not in captured.err
    finally:
        _pop_app_module()


# ---------------------------------------------------------------------------
# 7. Existing hardening (FLASK_SECRET_KEY / runtime / cookies) untouched
# ---------------------------------------------------------------------------

def test_secret_key_and_debug_hardening_still_enforced(app_module):
    assert app_module.app.secret_key == "gate-a-test-secret"
    assert app_module.app.config["DEBUG"] is False
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


# ---------------------------------------------------------------------------
# README.md / .env.example variable names match what app.py actually reads
# ---------------------------------------------------------------------------

REQUIRED_BOOTSTRAP_ENV_VARS = [
    "FLASK_SECRET_KEY",
    "BOOTSTRAP_ADMIN_ENABLED",
    "BOOTSTRAP_ADMIN_EMAIL",
    "BOOTSTRAP_ADMIN_PASSWORD",
]


def test_env_example_documents_the_real_bootstrap_variables():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in REQUIRED_BOOTSTRAP_ENV_VARS:
        assert re.search(rf"^{name}=", env_example, re.MULTILINE), (
            f"{name} missing from .env.example"
        )


def test_readme_documents_the_real_bootstrap_variables():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for name in REQUIRED_BOOTSTRAP_ENV_VARS:
        assert name in readme, f"{name} missing from README.md"


def test_documented_variables_are_the_ones_app_actually_reads():
    app_source = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    for name in REQUIRED_BOOTSTRAP_ENV_VARS:
        assert f'os.environ.get("{name}"' in app_source or f'"{name}"' in app_source, (
            f"{name} is documented but app.py does not appear to read it"
        )
