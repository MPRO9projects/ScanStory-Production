from datetime import datetime, timedelta


def test_registration_page_loads(client):
    response = client.get("/register")
    assert response.status_code == 200


def test_valid_registration_creates_user_trial_and_otp(client, app_module, isolated_app):
    response = client.post(
        "/register",
        data={
            "email": "new@example.com",
            "first_name": "New",
            "last_name": "User",
            "phone": "123",
            "password1": "password123",
            "password2": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    user = app_module.User.query.filter_by(email="new@example.com").first()
    assert user is not None
    assert user.trial_details is not None
    assert app_module.OTPCode.query.filter_by(email="new@example.com", purpose="verify_email").first() is not None
    assert isolated_app[1], "email seam should capture outgoing verification email"


def test_duplicate_registration_stays_on_register(client, normal_user):
    response = client.post(
        "/register",
        data={"email": normal_user.email, "password1": "password123", "password2": "password123"},
    )
    assert response.status_code == 200
    assert b"Email is already registered" in response.data


def test_email_verification_success(client, app_module, db_session):
    code = "123456"
    otp = app_module.OTPCode(
        email="verify@example.com",
        code=code,
        purpose="verify_email",
        expires_at=datetime.utcnow() + timedelta(minutes=2),
    )
    user = app_module.User(
        email="verify@example.com",
        password_hash="hash",
        is_verified=False,
        subscription_status="trial",
    )
    db_session.add_all([user, otp])
    db_session.commit()
    with client.session_transaction() as sess:
        sess["pending_verify_email"] = "verify@example.com"
    response = client.post("/verify-email/", data={"otp": code}, follow_redirects=False)
    assert response.status_code == 302
    assert app_module.User.query.filter_by(email="verify@example.com").first().is_verified is True


def test_login_success_sets_session(client, normal_user):
    response = client.post("/login/", data={"email": normal_user.email, "password": "password123"})
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess["user_id"] == normal_user.id


def test_login_failure_records_activity(client, app_module, normal_user):
    response = client.post("/login/", data={"email": normal_user.email, "password": "bad"})
    assert response.status_code == 200
    assert app_module.UserLoginActivity.query.filter_by(user_id=normal_user.id, is_successful=False).count() == 1


def test_logout_clears_user_session(client, login_user):
    response = client.get("/logout/", follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_expired_trial_redirects_to_subscribe(client, expired_user):
    response = client.post("/login/", data={"email": expired_user.email, "password": "password123"})
    assert response.status_code == 302
    assert "/subscribe" in response.headers["Location"]
