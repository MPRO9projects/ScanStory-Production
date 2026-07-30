from datetime import datetime, timedelta

from werkzeug.security import check_password_hash


def test_forgot_password_page_loads(client):
    assert client.get("/forgot-password/").status_code == 200


def test_valid_reset_request_creates_otp_and_email(client, app_module, normal_user, isolated_app):
    response = client.post("/forgot-password/", data={"email": normal_user.email}, follow_redirects=False)
    assert response.status_code == 302
    assert "/reset-password" in response.headers["Location"]
    assert app_module.OTPCode.query.filter_by(email=normal_user.email, purpose="reset_password").first() is not None
    assert isolated_app[1], "reset email should be captured by test seam"


def test_unknown_reset_request_does_not_create_otp(client, app_module):
    response = client.post("/forgot-password/", data={"email": "missing@example.com"}, follow_redirects=False)
    assert response.status_code == 302
    assert app_module.OTPCode.query.filter_by(email="missing@example.com", purpose="reset_password").first() is None


def test_reset_rejects_invalid_otp(client, normal_user):
    with client.session_transaction() as sess:
        sess["pending_reset_email"] = normal_user.email
    response = client.post(
        "/reset-password/",
        data={"otp": "000000", "new_password": "newpass123", "confirm_password": "newpass123"},
    )
    assert response.status_code == 200
    assert b"Invalid or expired OTP" in response.data


def test_reset_rejects_expired_otp(client, app_module, db_session, normal_user):
    db_session.add(app_module.OTPCode(
        email=normal_user.email,
        code="111111",
        purpose="reset_password",
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    ))
    db_session.commit()
    with client.session_transaction() as sess:
        sess["pending_reset_email"] = normal_user.email
    response = client.post(
        "/reset-password/",
        data={"otp": "111111", "new_password": "newpass123", "confirm_password": "newpass123"},
    )
    assert response.status_code == 200
    assert b"Invalid or expired OTP" in response.data


def test_valid_reset_updates_password_and_consumes_otp(client, app_module, db_session, normal_user):
    db_session.add(app_module.OTPCode(
        email=normal_user.email,
        code="222222",
        purpose="reset_password",
        expires_at=datetime.utcnow() + timedelta(minutes=2),
    ))
    db_session.commit()
    with client.session_transaction() as sess:
        sess["pending_reset_email"] = normal_user.email
    response = client.post(
        "/reset-password/",
        data={"otp": "222222", "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    refreshed = app_module.User.query.get(normal_user.id)
    assert check_password_hash(refreshed.password_hash, "newpass123")
    assert not check_password_hash(refreshed.password_hash, "password123")
    assert app_module.OTPCode.query.filter_by(email=normal_user.email, code="222222").first() is None
