import importlib
import sys

import pytest


pytestmark = pytest.mark.security


def _set_complete_production_env(monkeypatch, tmp_path):
    values = {
        "SCANSTORY_PRODUCTION": "1",
        "APP_ENV": "production",
        "FLASK_SECRET_KEY": "final-security-test-secret",
        "DATABASE_URL": "postgresql://db_user:db_password@db.example.test/scanstory",
        "SCANSTORY_QUEUE_MODE": "rq",
        "REDIS_URL": "redis://:redis_password@redis.example.test:6379/0",
        "SESSION_COOKIE_SECURE": "1",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user",
        "SMTP_PASS": "smtp-password",
        "MAIL_FROM": "no-reply@example.test",
        "RAZORPAY_KEY_ID": "rzp_test_key_id",
        "RAZORPAY_KEY_SECRET": "rzp_test_key_secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
        "SECURITY_CSP_ENABLED": "1",
        "SECURITY_CSP_ENFORCE": "1",
        "SCANSTORY_SKIP_STARTUP_BOOTSTRAP": "1",
        "SCANSTORY_DATA_DIR": str(tmp_path / "data"),
        "SCANSTORY_ADMIN_DATA_DIR": str(tmp_path / "admin_data"),
        "SCANSTORY_STATIC_UPLOADS_DIR": str(tmp_path / "static_uploads"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SCANSTORY_TESTING", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)


def _reload_app(monkeypatch, tmp_path, env_overrides=None):
    _set_complete_production_env(monkeypatch, tmp_path)
    for key, value in (env_overrides or {}).items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def test_development_runtime_allows_missing_razorpay_credentials(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "SCANSTORY_TESTING", False)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("FLASK_SECRET_KEY", "dev-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://dev:dev@localhost/scanstory")
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    for key in (
        "SCANSTORY_PRODUCTION", "APP_ENV", "ENV",
        "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    app_module._validate_required_runtime_config()


def test_production_requires_razorpay_configuration_without_leaking_values(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "SCANSTORY_TESTING", False)
    for key, value in {
        "SCANSTORY_PRODUCTION": "1",
        "FLASK_SECRET_KEY": "prod-secret",
        "DATABASE_URL": "postgresql://db_user:db_password@db.example.test/scanstory",
        "SCANSTORY_QUEUE_MODE": "rq",
        "REDIS_URL": "redis://:redis_password@redis.example.test:6379/0",
        "SESSION_COOKIE_SECURE": "1",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user",
        "SMTP_PASS": "smtp-password",
        "MAIL_FROM": "no-reply@example.test",
        "SECURITY_CSP_ENABLED": "1",
        "SECURITY_CSP_ENFORCE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError) as exc:
        app_module._validate_required_runtime_config()

    message = str(exc.value)
    assert "RAZORPAY_KEY_ID" in message
    assert "RAZORPAY_KEY_SECRET" in message
    assert "RAZORPAY_WEBHOOK_SECRET" in message
    assert "db_password" not in message
    assert "redis_password" not in message
    assert "smtp-password" not in message


def test_complete_production_payment_and_csp_config_passes_validation(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "SCANSTORY_TESTING", False)
    for key, value in {
        "SCANSTORY_PRODUCTION": "1",
        "FLASK_SECRET_KEY": "prod-secret",
        "DATABASE_URL": "postgresql://db_user:db_password@db.example.test/scanstory",
        "SCANSTORY_QUEUE_MODE": "rq",
        "REDIS_URL": "redis://:redis_password@redis.example.test:6379/0",
        "SESSION_COOKIE_SECURE": "1",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user",
        "SMTP_PASS": "smtp-password",
        "MAIL_FROM": "no-reply@example.test",
        "RAZORPAY_KEY_ID": "rzp_test_key_id",
        "RAZORPAY_KEY_SECRET": "rzp_test_key_secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
        "SECURITY_CSP_ENABLED": "1",
        "SECURITY_CSP_ENFORCE": "1",
    }.items():
        monkeypatch.setenv(key, value)

    app_module._validate_required_runtime_config()


def test_production_rejects_disabled_or_report_only_csp(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "SCANSTORY_TESTING", False)
    for key, value in {
        "SCANSTORY_PRODUCTION": "1",
        "FLASK_SECRET_KEY": "prod-secret",
        "DATABASE_URL": "postgresql://db_user:db_password@db.example.test/scanstory",
        "SCANSTORY_QUEUE_MODE": "rq",
        "REDIS_URL": "redis://localhost:6379/0",
        "SESSION_COOKIE_SECURE": "1",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user",
        "SMTP_PASS": "smtp-password",
        "MAIL_FROM": "no-reply@example.test",
        "RAZORPAY_KEY_ID": "rzp_test_key_id",
        "RAZORPAY_KEY_SECRET": "rzp_test_key_secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test_secret",
        "SECURITY_CSP_ENABLED": "1",
        "SECURITY_CSP_ENFORCE": "0",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError) as exc:
        app_module._validate_required_runtime_config()

    assert "SECURITY_CSP_ENFORCE=1" in str(exc.value)


def test_production_csp_enforces_by_default(monkeypatch, tmp_path):
    app_module = _reload_app(monkeypatch, tmp_path, {"SECURITY_CSP_ENFORCE": None})
    try:
        response = app_module.app.test_client().get("/healthz")
        assert response.status_code == 200
        assert "Content-Security-Policy" in response.headers
        assert "Content-Security-Policy-Report-Only" not in response.headers
    finally:
        sys.modules.pop("app", None)


def test_csp_header_contains_required_directives_and_no_secrets(client):
    response = client.get("/")
    header = response.headers.get("Content-Security-Policy-Report-Only") or response.headers.get("Content-Security-Policy")

    assert "default-src 'self'" in header
    assert "object-src 'none'" in header
    assert "base-uri 'self'" in header
    assert "frame-ancestors 'self'" in header
    assert "form-action 'self'" in header
    assert "https://checkout.razorpay.com" in header
    assert "https://api.razorpay.com" in header
    assert "https://www.google.com" in header
    assert "https://www.gstatic.com" in header
    assert "https://evil.example" not in header
    assert "rzp_test" not in header
    assert "secret" not in header.lower()


def test_ready_reports_payment_and_csp_readiness_without_secret_values(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://:redis_password@redis.example.test:6379/0")
    monkeypatch.setenv("SECURITY_CSP_ENABLED", "1")
    monkeypatch.setenv("SECURITY_CSP_ENFORCE", "1")
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: True)
    monkeypatch.setattr(app_module, "queue_worker_state", lambda: ("ok", 1))

    response = client.get("/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["queue"] == "ok"
    assert payload["checks"]["workers"] == "ok"
    assert payload["checks"]["payments"] == "unavailable"
    assert payload["checks"]["csp"] == "ok"
    body = response.get_data(as_text=True)
    assert "redis_password" not in body
    assert "RAZORPAY_KEY_SECRET" not in body


def test_healthz_remains_lightweight(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_readiness_checks", lambda: (_ for _ in ()).throw(AssertionError("deep check")))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_error_response_receives_csp_header(client):
    response = client.get("/missing-final-security-page")
    header = response.headers.get("Content-Security-Policy-Report-Only") or response.headers.get("Content-Security-Policy")

    assert response.status_code == 404
    assert header
    assert "object-src 'none'" in header


def test_scanner_page_receives_compatible_security_headers(client, project_with_pair):
    project, _pair = project_with_pair

    response = client.get(f"/scanner/{project.id}")
    header = response.headers.get("Content-Security-Policy-Report-Only") or response.headers.get("Content-Security-Policy")

    assert response.status_code == 200
    assert "camera=(self)" in response.headers["Permissions-Policy"]
    assert "blob:" in header
    assert "'wasm-unsafe-eval'" in header
    assert "https://checkout.razorpay.com" in header
