"""Production V1 Runtime Security Hardening - P0A.

Covers: debug/reloader defaults, mandatory FLASK_SECRET_KEY, safe error
responses, session cookie baseline, and removal of the hard-coded
add_simple_admin.py backdoor credential.
"""
import importlib
import sys

import pytest

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# 1. Debug / reloader defaults
# ---------------------------------------------------------------------------

def test_debug_defaults_to_false(app_module):
    assert app_module.app.config["DEBUG"] is False
    assert app_module.FLASK_DEBUG_ENABLED is False


def test_reloader_flag_defaults_to_false_via_env_helper(app_module):
    # use_reloader is passed straight from FLASK_DEBUG_ENABLED at app.run()
    # call time (not stored in app.config), so the env-parsing helper that
    # feeds it is exercised directly here.
    assert app_module._env_flag("FLASK_DEBUG_NOT_SET_XYZ", default=False) is False
    assert app_module._env_flag("FLASK_DEBUG_NOT_SET_XYZ") is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_env_flag_parses_booleans(app_module, monkeypatch, raw, expected):
    monkeypatch.setenv("SCANSTORY_HARDENING_TEST_FLAG", raw)
    assert app_module._env_flag("SCANSTORY_HARDENING_TEST_FLAG") is expected


def test_flask_debug_env_enables_debug_outside_testing(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "hardening-test-secret")
    monkeypatch.setenv("SCANSTORY_TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'debug-flag.db').as_posix()}")
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    try:
        app_module = importlib.import_module("app")
        assert app_module.FLASK_DEBUG_ENABLED is True
        assert app_module.app.config["DEBUG"] is True
    finally:
        for name in list(sys.modules):
            if name == "app":
                sys.modules.pop(name)


# ---------------------------------------------------------------------------
# 2. Mandatory FLASK_SECRET_KEY
# ---------------------------------------------------------------------------

def test_configured_secret_key_is_used(app_module):
    assert app_module.app.secret_key == "gate-a-test-secret"


def test_missing_secret_key_fails_clearly(monkeypatch, tmp_path):
    # app.py runs `from dotenv import load_dotenv; load_dotenv()` at import
    # time. Without this, deleting FLASK_SECRET_KEY from os.environ isn't
    # enough - the real repository .env (which has a real FLASK_SECRET_KEY)
    # gets loaded right back in during the fresh import below, silently
    # defeating the "missing key" condition this test exists to check.
    # Patching dotenv.load_dotenv before importing app is what makes this
    # work: app.py's `from dotenv import load_dotenv` re-binds the name from
    # the dotenv module at (re-)import time, and we force a fresh import via
    # the sys.modules removal below, so the patched no-op is what app.py
    # actually calls.
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'no-secret.db').as_posix()}")
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    try:
        with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
            importlib.import_module("app")
    finally:
        for name in list(sys.modules):
            if name == "app":
                sys.modules.pop(name)


# ---------------------------------------------------------------------------
# 3. Safe error responses
# ---------------------------------------------------------------------------

def test_api_path_500_does_not_leak_raw_exception(client, app_module, monkeypatch):
    @app_module.app.route("/api/_hardening_test_boom")
    def _boom():
        raise ValueError("super-secret-internal-detail /f/some/path")

    response = client.get("/api/_hardening_test_boom")
    assert response.status_code == 500
    body = response.get_json()
    assert body["error"] is True
    assert body["detected"] is False
    assert "super-secret-internal-detail" not in response.get_data(as_text=True)
    assert "/f/some/path" not in response.get_data(as_text=True)


def test_html_route_500_does_not_leak_raw_exception(client, app_module):
    @app_module.app.route("/_hardening_test_boom_html")
    def _boom_html():
        raise ValueError("super-secret-internal-detail /f/some/path")

    response = client.get("/_hardening_test_boom_html")
    assert response.status_code == 500
    text = response.get_data(as_text=True)
    assert "super-secret-internal-detail" not in text
    assert "/f/some/path" not in text
    assert "Traceback" not in text


def test_exception_details_are_logged(client, app_module, caplog):
    @app_module.app.route("/api/_hardening_test_boom_logged")
    def _boom_logged():
        raise ValueError("logged-detail-marker-12345")

    import logging
    with caplog.at_level(logging.ERROR, logger=app_module.app.logger.name):
        client.get("/api/_hardening_test_boom_logged")
    assert any("logged-detail-marker-12345" in record.getMessage() or
               (record.exc_info and "logged-detail-marker-12345" in str(record.exc_info[1]))
               for record in caplog.records)


def test_404_response_is_generic_but_useful(client):
    response = client.get("/this-route-does-not-exist-hardening-test")
    assert response.status_code == 404
    text = response.get_data(as_text=True)
    assert "404" in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# 5. Session cookie baseline
# ---------------------------------------------------------------------------

def test_development_cookie_config_allows_http(app_module):
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # Default (no SESSION_COOKIE_SECURE env set) keeps localhost HTTP dev working.
    assert app_module.app.config["SESSION_COOKIE_SECURE"] is False


def test_production_cookie_config_can_require_secure(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "hardening-test-secret")
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'secure-cookie.db').as_posix()}")
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    try:
        app_module = importlib.import_module("app")
        assert app_module.app.config["SESSION_COOKIE_SECURE"] is True
        assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    finally:
        for name in list(sys.modules):
            if name == "app":
                sys.modules.pop(name)


# ---------------------------------------------------------------------------
# 4. add_simple_admin.py hard-coded credential removal
# ---------------------------------------------------------------------------

def test_hardcoded_admin_credential_is_not_a_live_account(client, app_module):
    response = client.post(
        "/admin/login",
        data={"email": "admin@gmail.com", "password": "admin123"},
    )
    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert "admin_id" not in sess
    assert app_module.Admin.query.filter_by(email="admin@gmail.com").first() is None


def _reimport_add_simple_admin():
    # add_simple_admin binds `app`/`db` at import time; force a fresh bind
    # to whichever app_module the current test's isolated_app fixture set up,
    # rather than reusing a stale module cached from an earlier test.
    for name in list(sys.modules):
        if name == "add_simple_admin":
            sys.modules.pop(name)
    return importlib.import_module("add_simple_admin")


def test_add_simple_admin_refuses_without_required_env(app_module, monkeypatch):
    add_simple_admin = _reimport_add_simple_admin()
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CONFIRM_ADMIN_CREATION", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        add_simple_admin.main()
    assert exc_info.value.code != 0


def test_add_simple_admin_creates_no_default_credential_when_run(app_module, monkeypatch):
    add_simple_admin = _reimport_add_simple_admin()
    monkeypatch.setenv("ADMIN_EMAIL", "operator-chosen@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-real-operator-password")
    monkeypatch.setenv("CONFIRM_ADMIN_CREATION", "1")
    add_simple_admin.main()
    created = app_module.Admin.query.filter_by(email="operator-chosen@example.com").first()
    assert created is not None
    assert app_module.Admin.query.filter_by(email="admin@gmail.com").first() is None
