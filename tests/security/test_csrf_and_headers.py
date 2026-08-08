"""P0B: CSRF protection + HTTP security header baseline.

CSRF is now enabled globally in app.py (WTF_CSRF_ENABLED=True). The
project's standard `app`/`client` fixtures (tests/conftest.py:isolated_app)
deliberately override this back to False for the rest of the test suite -
a standard Flask-WTF testing convention so ordinary route tests don't need
to thread a token through every form post. This file exists specifically
to prove the REAL, production enforcement path: a fresh app import that
does NOT apply that override.
"""
import importlib
import re
import sys

import pytest
from werkzeug.security import generate_password_hash

pytestmark = pytest.mark.security


def _reimport_app_with_real_csrf(monkeypatch, tmp_path, extra_env=None):
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'csrf.db').as_posix()}")
    monkeypatch.setenv("SCANSTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCANSTORY_ADMIN_DATA_DIR", str(tmp_path / "data_admin"))
    monkeypatch.setenv("SCANSTORY_STATIC_UPLOADS_DIR", str(tmp_path / "static_uploads"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "csrf-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_ENABLED", raising=False)
    for key, value in (extra_env or {}).items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    app_module = importlib.import_module("app")
    # Deliberately NOT forcing WTF_CSRF_ENABLED=False here - real enforcement is the point.
    app_module.app.config.update(TESTING=True)
    return app_module


def _pop_app_module():
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)


def _extract_hidden_csrf_token(html_text):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html_text)
    assert match, "no csrf_token hidden input found in response HTML"
    return match.group(1)


def _extract_js_csrf_token(html_text):
    match = re.search(r"'X-CSRFToken':\s*'([^']+)'", html_text)
    assert match, "no X-CSRFToken JS literal found in response HTML"
    return match.group(1)


@pytest.fixture()
def csrf_app(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path)
    with app_module.app.app_context():
        app_module.db.create_all()
        app_module.ensure_marker_schema()
        app_module.bootstrap_database()
        app_module.db.session.commit()
        yield app_module
        app_module.db.session.remove()
        app_module.db.drop_all()
    _pop_app_module()


@pytest.fixture()
def csrf_client(csrf_app):
    return csrf_app.app.test_client()


def _make_trial_user(csrf_app, email="csrfuser@example.com", password="password123"):
    with csrf_app.app.app_context():
        plan = csrf_app.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
        user = csrf_app.User(
            email=email,
            password_hash=generate_password_hash(password),
            is_verified=True,
            subscription_id=plan.id,
            subscription_status="trial",
            subscribed_project_limit=plan.total_project_limit,
            subscribed_scan_limit=plan.total_scan_limit,
        )
        csrf_app.db.session.add(user)
        csrf_app.db.session.commit()
        return user.id


# ---------------------------------------------------------------------------
# 1-2: browser HTML form - rejected without a token, succeeds with one
# ---------------------------------------------------------------------------

def test_post_form_without_csrf_token_rejected(csrf_client):
    response = csrf_client.post("/login/", data={"email": "nobody@example.com", "password": "whatever"})
    assert response.status_code == 400
    assert b"could not be verified" in response.data or b"expired" in response.data


def test_post_form_with_valid_csrf_token_succeeds(csrf_client, csrf_app):
    user_id = _make_trial_user(csrf_app)
    get_resp = csrf_client.get("/login/")
    token = _extract_hidden_csrf_token(get_resp.get_data(as_text=True))
    response = csrf_client.post(
        "/login/",
        data={"email": "csrfuser@example.com", "password": "password123", "csrf_token": token},
    )
    assert response.status_code == 302
    with csrf_client.session_transaction() as sess:
        assert sess["user_id"] == user_id


# ---------------------------------------------------------------------------
# 3-4: AJAX mutation - rejected without header token, succeeds with it
# ---------------------------------------------------------------------------

def test_ajax_mutation_without_csrf_header_rejected(csrf_client, csrf_app):
    user_id = _make_trial_user(csrf_app)
    with csrf_app.app.app_context():
        paid_plan = csrf_app.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
        plan_id = paid_plan.id
    with csrf_client.session_transaction() as sess:
        sess["user_id"] = user_id
    response = csrf_client.post("/create-razorpay-order", data={"plan_id": plan_id})
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] is True
    assert "Traceback" not in response.get_data(as_text=True)


def test_ajax_mutation_with_valid_csrf_header_succeeds(csrf_client, csrf_app, monkeypatch):
    user_id = _make_trial_user(csrf_app)
    with csrf_app.app.app_context():
        paid_plan = csrf_app.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
        plan_id = paid_plan.id

    class _FakeRazorpayOrder:
        def create(self, data):
            return {"id": "order_csrf_test_123", **data}

    class _FakeRazorpayClient:
        def __init__(self):
            self.order = _FakeRazorpayOrder()

    monkeypatch.setattr(csrf_app, "razorpay_client", _FakeRazorpayClient())
    monkeypatch.setattr(csrf_app, "RAZORPAY_KEY_ID", "rzp_test_key")

    with csrf_client.session_transaction() as sess:
        sess["user_id"] = user_id
    # /subscribe renders the same JS X-CSRFToken literal used by the real fetch() call.
    page = csrf_client.get("/subscribe")
    token = _extract_js_csrf_token(page.get_data(as_text=True))
    response = csrf_client.post(
        "/create-razorpay-order",
        data={"plan_id": plan_id},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True


# ---------------------------------------------------------------------------
# 4b: admin mutation form (new /admin/capacity route, V1 Agent 2 task 2) -
# proves the same global CSRFProtect enforcement covers admin POST forms too,
# not just the user-facing routes exercised above.
# ---------------------------------------------------------------------------

def _make_superadmin(csrf_app, email="capacity-admin@example.com"):
    with csrf_app.app.app_context():
        admin_obj = csrf_app.Admin(
            email=email,
            name="Capacity Admin",
            password_hash=generate_password_hash("AdminPass123"),
            role="superadmin",
            is_active=True,
        )
        csrf_app.db.session.add(admin_obj)
        csrf_app.db.session.commit()
        return admin_obj.id


def test_admin_capacity_post_without_csrf_token_rejected(csrf_client, csrf_app):
    admin_id = _make_superadmin(csrf_app)
    with csrf_client.session_transaction() as sess:
        sess["admin_id"] = admin_id
    response = csrf_client.post("/admin/capacity", data={"configured_limit": "50", "enabled": "on"})
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert "could not be verified" in text or "expired" in text


def test_admin_capacity_post_with_valid_csrf_token_succeeds(csrf_client, csrf_app):
    admin_id = _make_superadmin(csrf_app, email="capacity-admin2@example.com")
    with csrf_client.session_transaction() as sess:
        sess["admin_id"] = admin_id
    page = csrf_client.get("/admin/capacity")
    token = _extract_hidden_csrf_token(page.get_data(as_text=True))
    response = csrf_client.post(
        "/admin/capacity",
        data={"configured_limit": "77", "enabled": "on", "csrf_token": token},
    )
    assert response.status_code == 302
    with csrf_app.app.app_context():
        config = csrf_app.CapacityConfig.query.get(1)
        assert config.configured_limit == 77
        assert config.enabled is True


# ---------------------------------------------------------------------------
# 5: justified scanner exemption still works, unauthenticated, no token
# ---------------------------------------------------------------------------

def test_scanner_endpoint_exempt_from_csrf(csrf_client):
    response = csrf_client.post("/detect_init", data={})
    # Whatever detect_init itself decides about this malformed request is
    # its own business logic - the only thing under test is that CSRF
    # protection did not intercept it first.
    assert "could not be verified" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 6: CSRF failure responses never leak internals
# ---------------------------------------------------------------------------

def test_csrf_failure_does_not_expose_internal_exception(csrf_client):
    response = csrf_client.post("/login/", data={"email": "x@example.com", "password": "y"})
    text = response.get_data(as_text=True)
    assert "Traceback" not in text
    assert "flask_wtf" not in text
    assert "CSRFError" not in text
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 7-8: security headers on normal responses, camera not blocked for scanner
# ---------------------------------------------------------------------------

def test_security_headers_present_on_normal_response(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    # CSP defaults to report-only until SECURITY_CSP_ENFORCE=1 is explicitly
    # set (see CSP staged-rollout tests below) - not enforced here.
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy-Report-Only"]


def test_permissions_policy_allows_camera_for_self(client):
    response = client.get("/")
    policy = response.headers["Permissions-Policy"]
    assert "camera=(self)" in policy
    assert "microphone=()" in policy


# ---------------------------------------------------------------------------
# CSP staged rollout: report-only by default, enforce/disable via env
# ---------------------------------------------------------------------------

def test_csp_is_report_only_by_default(client):
    response = client.get("/")
    assert "Content-Security-Policy-Report-Only" in response.headers
    assert "Content-Security-Policy" not in response.headers


def test_csp_enforcing_only_when_explicitly_enabled(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path, {"SECURITY_CSP_ENFORCE": "1"})
    try:
        client = app_module.app.test_client()
        response = client.get("/")
        assert "Content-Security-Policy" in response.headers
        assert "Content-Security-Policy-Report-Only" not in response.headers
        assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
    finally:
        _pop_app_module()


def test_csp_disabled_sends_neither_header(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path, {"SECURITY_CSP_ENABLED": "0"})
    try:
        client = app_module.app.test_client()
        response = client.get("/")
        assert "Content-Security-Policy" not in response.headers
        assert "Content-Security-Policy-Report-Only" not in response.headers
    finally:
        _pop_app_module()


def test_csp_disabled_still_enforces_other_headers(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path, {"SECURITY_CSP_ENABLED": "0"})
    try:
        client = app_module.app.test_client()
        response = client.get("/")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "camera=(self)" in response.headers["Permissions-Policy"]
    finally:
        _pop_app_module()


# ---------------------------------------------------------------------------
# 9-10: HSTS gating (unchanged by the CSP staged-rollout correction)
# ---------------------------------------------------------------------------

def test_hsts_absent_over_ordinary_local_http_by_default(client):
    response = client.get("/")
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_absent_even_when_enabled_but_request_is_plain_http(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path, {"SECURITY_HSTS_ENABLED": "1"})
    try:
        client = app_module.app.test_client()
        response = client.get("/")
        assert "Strict-Transport-Security" not in response.headers
    finally:
        _pop_app_module()


def test_hsts_present_only_when_enabled_and_genuinely_https(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path, {"SECURITY_HSTS_ENABLED": "1"})
    try:
        client = app_module.app.test_client()
        response = client.get("/", environ_overrides={"wsgi.url_scheme": "https"})
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    finally:
        _pop_app_module()
