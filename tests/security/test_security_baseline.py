from io import BytesIO

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


def test_upload_should_require_file_signature_validation(client, app_module, login_user):
    """P0D: uploads are validated from real content, not extension/MIME.

    Full scenario coverage lives in
    tests/security/test_upload_validation.py; this proves the exact concern
    the xfail used to document is now genuinely closed.
    """
    data = {
        "name": "Signature Check Project",
        "upload_id": "baseline-signature-check",
        "images": [(BytesIO(b"MZ\x90\x00fake-executable-payload"), "marker.jpg")],
        "videos": [(BytesIO(b"video"), "clip.mp4")],
        "marker_0_mode": "crop",
        "marker_0_crop_x": "0.1",
        "marker_0_crop_y": "0.1",
        "marker_0_crop_width": "0.6",
        "marker_0_crop_height": "0.6",
        "marker_0_rotation": "0",
        "marker_0_original_width": "640",
        "marker_0_original_height": "480",
        "marker_0_processed_width": "520",
        "marker_0_processed_height": "420",
        "marker_0_source_size_bytes": "100000",
        "marker_0_processed_size_bytes": "90000",
        "marker_0_display_orientation": "landscape",
    }
    before_count = app_module.Project.query.count()
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert app_module.Project.query.count() == before_count


@pytest.mark.xfail(reason="severity=High; flow=OTP verification; desired=rate limit failed OTP attempts; actual=no OTP brute-force throttling; future_gate=auth security")
def test_otp_bruteforce_throttling_required():
    raise AssertionError("OTP brute-force throttling not implemented")
