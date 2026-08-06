from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


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


def test_unverified_login_redirects_to_verify_without_user_session(client, app_module, db_session, plan):
    user = app_module.User(
        email="unverified@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/login/",
        data={"email": user.email, "password": "password123"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/verify-email/" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "user_email" not in sess
        assert sess["pending_verify_email"] == user.email


def test_unverified_login_preserves_invalid_password_behavior(client, app_module, db_session, plan):
    user = app_module.User(
        email="unverified-bad-password@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post("/login/", data={"email": user.email, "password": "wrong"})

    assert response.status_code == 200
    assert b"Invalid email or password" in response.data
    assert app_module.UserLoginActivity.query.filter_by(user_id=user.id, is_successful=False).count() == 1
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "pending_verify_email" not in sess


def test_unverified_login_preserves_blocked_user_behavior(client, app_module, db_session, plan):
    user = app_module.User(
        email="blocked-unverified@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        is_blocked=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post("/login/", data={"email": user.email, "password": "password123"})

    assert response.status_code == 200
    assert b"Your account is blocked" in response.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "pending_verify_email" not in sess


def test_login_lockout_still_runs_before_verification_gate(client, app_module, db_session, plan):
    user = app_module.User(
        email="locked-unverified@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()
    for _ in range(4):
        db_session.add(app_module.UserLoginActivity(
            user_id=user.id,
            ip_address="127.0.0.1",
            user_agent="pytest",
            is_successful=False,
            login_at=datetime.utcnow(),
        ))
    db_session.commit()

    response = client.post("/login/", data={"email": user.email, "password": "password123"})

    assert response.status_code == 200
    assert b"Account temporarily locked" in response.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "pending_verify_email" not in sess


def test_login_required_rejects_stale_unverified_session(client, app_module, db_session, plan):
    user = app_module.User(
        email="stale-unverified@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
        sess["user_email"] = user.email

    response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 302
    assert "/verify-email/" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "user_email" not in sess
        assert sess["pending_verify_email"] == user.email


def test_login_required_allows_verified_normal_session(client, normal_user):
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
        sess["user_email"] = normal_user.email

    response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 200


def test_admin_login_behavior_unchanged(client, admin, admin_password):
    response = client.post("/admin/login", data={"email": admin.email, "password": admin_password})

    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess["admin_id"] == admin.id
        assert "user_id" not in sess


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
