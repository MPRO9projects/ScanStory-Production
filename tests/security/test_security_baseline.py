import pytest


pytestmark = pytest.mark.security


@pytest.mark.xfail(reason="Gate A documents current gap: security header helper is not registered as an after_request hook")
def test_security_headers_present(client):
    response = client.get("/")
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_admin_route_requires_admin_session(client):
    response = client.get("/admin/users")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_user_cannot_view_another_user_project(client, app_module, db_session, login_user, plan):
    other = app_module.User(
        email="other@example.com",
        password_hash="hash",
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=1,
        subscribed_scan_limit=1,
    )
    db_session.add(other)
    db_session.commit()
    project = app_module.Project(name="Other Project", owner_user_id=other.id)
    db_session.add(project)
    db_session.commit()
    response = client.get(f"/project/{project.id}")
    assert response.status_code == 404


@pytest.mark.xfail(reason="Gate A documents current gap: CSRF is disabled globally in current app config")
def test_csrf_should_be_enabled_for_mutating_routes(app):
    assert app.config.get("WTF_CSRF_ENABLED") is True


@pytest.mark.xfail(reason="Gate A documents current gap: upload validation does not fully verify file signatures before save")
def test_upload_should_require_file_signature_validation():
    raise AssertionError("File signature validation is not yet enforced")


@pytest.mark.xfail(reason="Gate A documents current gap: OTP brute-force throttling is not enforced")
def test_otp_bruteforce_throttling_required():
    raise AssertionError("OTP brute-force throttling not implemented")
