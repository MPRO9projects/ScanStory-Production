import pytest


pytestmark = pytest.mark.security


def test_security_headers_present(client):
    """P0B: add_security_headers is now registered via app.after_request."""
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


# P0B: CSRF is now enabled globally in app.py. The standard `app`/`client`
# fixtures here intentionally set WTF_CSRF_ENABLED=False (see
# tests/conftest.py:isolated_app) so the rest of this ordinary test suite
# doesn't need to thread tokens through every form post - that's a
# deliberate, standard Flask-WTF testing convention, not a regression.
# Real enforcement (real app boot, real 400 on missing/invalid token, real
# 200 on a valid one) is proven in tests/security/test_csrf_and_headers.py.


@pytest.mark.xfail(reason="severity=High; flow=upload; desired=file signatures verified before save; actual=upload path does not enforce signature validation; future_gate=upload security")
def test_upload_should_require_file_signature_validation():
    raise AssertionError("File signature validation is not yet enforced")


@pytest.mark.xfail(reason="severity=High; flow=OTP verification; desired=rate limit failed OTP attempts; actual=no OTP brute-force throttling; future_gate=auth security")
def test_otp_bruteforce_throttling_required():
    raise AssertionError("OTP brute-force throttling not implemented")
