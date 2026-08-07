from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from werkzeug.security import check_password_hash, generate_password_hash


pytestmark = pytest.mark.security


def _issue_otp(app_module, email, purpose="verify_email", user_id=None):
    with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.10"}):
        code = app_module._create_otp(email, purpose, user_id=user_id)
    rec = app_module._latest_otp(email, purpose)
    return code, rec


def _set_verify_session(client, email, challenge_id):
    with client.session_transaction() as sess:
        sess["pending_verify_email"] = email
        sess["pending_verify_challenge_id"] = challenge_id


def _set_user_reset_session(client, email, challenge_id):
    with client.session_transaction() as sess:
        sess["pending_reset_email"] = email
        sess["pending_reset_challenge_id"] = challenge_id


def _set_admin_reset_session(client, email, challenge_id):
    with client.session_transaction() as sess:
        sess["pending_admin_reset_email"] = email
        sess["pending_admin_reset_challenge_id"] = challenge_id


def _recreate_legacy_otp_table(app_module, db_session):
    db_session.execute(app_module.text("DROP TABLE otp_codes"))
    db_session.execute(app_module.text("""
        CREATE TABLE otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR(255) NOT NULL,
            code VARCHAR(6) NOT NULL,
            purpose VARCHAR(50) NOT NULL,
            expires_at DATETIME NOT NULL,
            is_used BOOLEAN,
            used_at DATETIME,
            ip_address VARCHAR(45),
            user_agent TEXT,
            user_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    """))
    db_session.commit()


def _make_user(app_module, db_session, email):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=False,
        subscription_status="trial",
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_purpose_and_account_binding_across_user_and_admin_flows(app_module, db_session, normal_user, admin):
    other = _make_user(app_module, db_session, "other@example.com")
    verify_code, verify_rec = _issue_otp(app_module, normal_user.email, "verify_email", user_id=normal_user.id)
    reset_code, reset_rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    admin_code, admin_rec = _issue_otp(app_module, admin.email, "admin_reset_password")

    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(normal_user.email, "reset_password", verify_code, challenge_id=verify_rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", reset_code, challenge_id=reset_rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "reset_password", admin_code, challenge_id=admin_rec.challenge_id) is False
        assert app_module._verify_otp(admin.email, "admin_reset_password", reset_code, challenge_id=reset_rec.challenge_id) is False
        assert app_module._verify_otp(other.email, "verify_email", verify_code, challenge_id=verify_rec.challenge_id) is False
        assert app_module._verify_otp(other.email, "verify_email", verify_code, challenge_id=verify_rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", verify_code, challenge_id=reset_rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", verify_code) is False


def test_user_password_reset_final_request_is_single_use(client, app_module, db_session, normal_user):
    code, rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    _set_user_reset_session(client, normal_user.email, rec.challenge_id)

    first = client.post(
        "/reset-password/",
        data={"otp": code, "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=False,
    )
    second = client.post(
        "/reset-password/",
        data={"otp": code, "new_password": "otherpass123", "confirm_password": "otherpass123"},
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert "/login" in first.headers["Location"]
    assert second.status_code == 302
    assert "/forgot-password" in second.headers["Location"]
    assert check_password_hash(app_module.User.query.get(normal_user.id).password_hash, "newpass123")


def test_admin_password_reset_final_request_is_single_use(client, app_module, db_session, admin):
    code, rec = _issue_otp(app_module, admin.email, "admin_reset_password")
    _set_admin_reset_session(client, admin.email, rec.challenge_id)

    first = client.post(
        "/admin/reset-password",
        data={"otp": code, "new_password": "AdminPass123", "confirm_password": "AdminPass123"},
        follow_redirects=False,
    )
    second = client.post(
        "/admin/reset-password",
        data={"otp": code, "new_password": "AdminPass456", "confirm_password": "AdminPass456"},
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert "/admin/login" in first.headers["Location"]
    assert second.status_code == 302
    assert "/admin/forgot-password" in second.headers["Location"]
    assert check_password_hash(app_module.Admin.query.get(admin.id).password_hash, "AdminPass123")


def test_concurrent_user_password_reset_allows_at_most_one_success(app_module, db_session, normal_user):
    code, rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    email = normal_user.email
    challenge_id = rec.challenge_id
    barrier = Barrier(2)

    def reset_to(password):
        client = app_module.app.test_client()
        _set_user_reset_session(client, email, challenge_id)
        barrier.wait()
        response = client.post(
            "/reset-password/",
            data={"otp": code, "new_password": password, "confirm_password": password},
            follow_redirects=False,
        )
        return response.status_code, response.headers.get("Location", ""), password

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [
            executor.submit(reset_to, "newpass123"),
            executor.submit(reset_to, "newpass456"),
        ]]

    successes = [password for _status, location, password in results if "/login" in location]
    assert len(successes) == 1
    db_session.expire_all()
    assert check_password_hash(app_module.User.query.get(normal_user.id).password_hash, successes[0])


def test_successful_password_reset_invalidates_previous_reset_challenges(client, app_module, db_session, normal_user):
    old_code, old_rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    new_code, new_rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    _set_user_reset_session(client, normal_user.email, new_rec.challenge_id)

    response = client.post(
        "/reset-password/",
        data={"otp": new_code, "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db_session.expire_all()
    assert app_module.OTPCode.query.get(old_rec.id).invalidated_at is not None
    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(normal_user.email, "reset_password", old_code, challenge_id=old_rec.challenge_id) is False


def test_user_and_admin_reset_session_state_do_not_overlap(client, app_module, normal_user, admin):
    user_code, user_rec = _issue_otp(app_module, normal_user.email, "reset_password", user_id=normal_user.id)
    admin_code, admin_rec = _issue_otp(app_module, admin.email, "admin_reset_password")
    with client.session_transaction() as sess:
        sess["pending_reset_email"] = normal_user.email
        sess["pending_reset_challenge_id"] = user_rec.challenge_id
        sess["pending_admin_reset_email"] = admin.email
        sess["pending_admin_reset_challenge_id"] = admin_rec.challenge_id

    user_response = client.post(
        "/reset-password/",
        data={"otp": admin_code, "new_password": "newpass123", "confirm_password": "newpass123"},
    )
    admin_response = client.post(
        "/admin/reset-password",
        data={"otp": user_code, "new_password": "AdminPass123", "confirm_password": "AdminPass123"},
    )

    assert user_response.status_code == 200
    assert admin_response.status_code == 200
    assert b"Invalid or expired OTP" in user_response.data
    assert b"Invalid or expired OTP" in admin_response.data


def test_valid_otp_verifies_once_and_cannot_replay(client, app_module, db_session, normal_user):
    normal_user.is_verified = False
    db_session.commit()
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    _set_verify_session(client, normal_user.email, rec.challenge_id)

    first = client.post("/verify-email/", data={"otp": code}, follow_redirects=False)
    second = client.post("/verify-email/", data={"otp": code}, follow_redirects=False)

    assert first.status_code == 302
    assert second.status_code == 302
    db_session.expire_all()
    used = app_module.OTPCode.query.get(rec.id)
    assert used.is_used is True
    assert used.used_at is not None
    assert used.invalidated_at is not None


def test_new_otp_is_hashed_not_plaintext(app_module, normal_user):
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)

    assert rec.code != code
    assert rec.code_hash
    assert check_password_hash(rec.code_hash, code)


def test_wrong_otp_increments_attempts_and_locks_at_max(app_module, db_session, normal_user):
    app_module.OTP_MAX_VERIFY_ATTEMPTS = 2
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)

    with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.10"}):
        assert app_module._verify_otp(normal_user.email, "verify_email", "000000", challenge_id=rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", "111111", challenge_id=rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", code, challenge_id=rec.challenge_id) is False

    db_session.expire_all()
    locked = app_module.OTPCode.query.get(rec.id)
    assert locked.attempt_count == 2
    assert locked.locked_until is not None
    assert locked.invalidated_at is not None


def test_expired_otp_is_rejected(app_module, db_session, normal_user):
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    rec.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(normal_user.email, "verify_email", code, challenge_id=rec.challenge_id) is False


def test_resend_before_minimum_interval_is_blocked_without_smtp(app_module, monkeypatch, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 60
    _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    sends = []

    with app_module.app.test_request_context("/resend-otp/"):
        sent, message = app_module._resend_otp(
            normal_user.email,
            "verify_email",
            lambda *_args, **_kwargs: sends.append("sent"),
            user_id=normal_user.id,
        )

    assert sent is False
    assert sends == []
    assert "shortly" in message


def test_resend_limit_enforced_without_smtp(app_module, db_session, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    app_module.OTP_MAX_RESENDS = 1
    _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    sends = []

    with app_module.app.test_request_context("/resend-otp/"):
        first_sent, _ = app_module._resend_otp(
            normal_user.email,
            "verify_email",
            lambda *_args, **_kwargs: sends.append("sent"),
            user_id=normal_user.id,
        )
        second_sent, _ = app_module._resend_otp(
            normal_user.email,
            "verify_email",
            lambda *_args, **_kwargs: sends.append("sent"),
            user_id=normal_user.id,
        )

    assert first_sent is True
    assert second_sent is False
    assert sends == ["sent"]


def test_successful_resend_invalidates_old_otp_and_new_otp_succeeds(app_module, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    old_code, old_rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    delivered = []

    def send(_email, code, **_kwargs):
        delivered.append(code)

    with app_module.app.test_request_context("/resend-otp/"):
        sent, _ = app_module._resend_otp(normal_user.email, "verify_email", send, user_id=normal_user.id)
        new_rec = app_module._latest_otp(normal_user.email, "verify_email")
        assert sent is True
        assert app_module._verify_otp(normal_user.email, "verify_email", old_code, challenge_id=old_rec.challenge_id) is False
        assert app_module._verify_otp(normal_user.email, "verify_email", delivered[-1], challenge_id=new_rec.challenge_id) is True


def test_resend_otp_get_does_not_mutate_state(client, app_module, normal_user):
    _old_code, old_rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    with client.session_transaction() as sess:
        sess["pending_verify_email"] = normal_user.email
        sess["pending_verify_challenge_id"] = old_rec.challenge_id

    response = client.get("/resend-otp/", follow_redirects=False)

    assert response.status_code == 405
    assert app_module.OTPCode.query.filter_by(email=normal_user.email, purpose="verify_email").count() == 1
    with client.session_transaction() as sess:
        assert sess["pending_verify_challenge_id"] == old_rec.challenge_id


def test_resend_otp_post_sets_new_device_challenge(client, app_module, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    old_code, old_rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    with client.session_transaction() as sess:
        sess["pending_verify_email"] = normal_user.email

    response = client.post("/resend-otp/", follow_redirects=False)

    assert response.status_code == 302
    new_rec = app_module._latest_otp(normal_user.email, "verify_email")
    assert new_rec.id != old_rec.id
    assert app_module.OTPCode.query.get(old_rec.id).invalidated_at is not None
    with client.session_transaction() as sess:
        assert sess["pending_verify_challenge_id"] == new_rec.challenge_id
    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(normal_user.email, "verify_email", old_code, challenge_id=old_rec.challenge_id) is False


def test_smtp_failure_does_not_corrupt_existing_verification_state(app_module, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    old_code, old_rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)

    def fail_send(*_args, **_kwargs):
        raise RuntimeError("smtp down")

    with app_module.app.test_request_context("/resend-otp/"):
        sent, _ = app_module._resend_otp(normal_user.email, "verify_email", fail_send, user_id=normal_user.id)
        assert sent is False
        assert app_module._verify_otp(normal_user.email, "verify_email", old_code, challenge_id=old_rec.challenge_id) is True


def test_concurrent_resend_only_one_email_and_one_active_challenge(app_module, db_session, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    app_module.OTP_MAX_RESENDS = 1
    _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    email = normal_user.email
    sends = []
    barrier = Barrier(2)

    def resend():
        with app_module.app.test_request_context("/resend-otp/"):
            barrier.wait()
            return app_module._resend_otp(
                email,
                "verify_email",
                lambda _email, code, **_kwargs: sends.append(code),
                user_id=normal_user.id,
            )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(resend), executor.submit(resend)]]

    assert sorted(results) == [False, True]
    assert len(sends) == 1
    db_session.expire_all()
    active = app_module.OTPCode.query.filter_by(email=email, purpose="verify_email", is_used=False).filter(
        app_module.OTPCode.invalidated_at.is_(None)
    ).all()
    assert len(active) == 1


def test_concurrent_resend_smtp_failure_preserves_previous_challenge(app_module, db_session, normal_user):
    app_module.OTP_RESEND_MIN_INTERVAL_SECONDS = 0
    app_module.OTP_MAX_RESENDS = 1
    old_code, old_rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    email = normal_user.email
    barrier = Barrier(2)

    def resend_fail():
        with app_module.app.test_request_context("/resend-otp/"):
            barrier.wait()
            return app_module._resend_otp(
                email,
                "verify_email",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("smtp down")),
                user_id=normal_user.id,
            )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(resend_fail), executor.submit(resend_fail)]]

    assert results == [False, False]
    db_session.expire_all()
    previous = app_module.OTPCode.query.get(old_rec.id)
    assert previous.invalidated_at is None
    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(email, "verify_email", old_code, challenge_id=old_rec.challenge_id) is True


def test_forgot_password_response_is_generic_for_missing_email(client, app_module, isolated_app):
    response = client.post("/forgot-password/", data={"email": "missing@example.com"}, follow_redirects=True)

    assert response.status_code == 200
    assert b"If the email exists, an OTP has been sent." in response.data
    assert app_module.OTPCode.query.filter_by(email="missing@example.com").first() is None
    assert isolated_app[1] == []


def test_known_and_unknown_forgot_password_responses_are_generic(client, normal_user):
    known = client.post("/forgot-password/", data={"email": normal_user.email}, follow_redirects=True)
    unknown = client.post("/forgot-password/", data={"email": "missing@example.com"}, follow_redirects=True)

    assert known.status_code == unknown.status_code == 200
    assert b"If the email exists, an OTP has been sent." in known.data
    assert b"If the email exists, an OTP has been sent." in unknown.data


def test_known_and_unknown_admin_forgot_password_responses_are_generic(client, admin):
    known = client.post("/admin/forgot-password", data={"email": admin.email}, follow_redirects=True)
    unknown = client.post("/admin/forgot-password", data={"email": "missing-admin@example.com"}, follow_redirects=True)

    assert known.status_code == unknown.status_code == 200
    assert b"If an admin account exists with this email" in known.data
    assert b"If an admin account exists with this email" in unknown.data


def test_concurrent_verification_requests_result_in_one_success(app_module, db_session, normal_user):
    normal_user.is_verified = False
    db_session.commit()
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    email = normal_user.email
    challenge_id = rec.challenge_id
    barrier = Barrier(2)

    def verify():
        client = app_module.app.test_client()
        _set_verify_session(client, email, challenge_id)
        barrier.wait()
        response = client.post("/verify-email/", data={"otp": code}, follow_redirects=False)
        return response.status_code, response.headers.get("Location", "")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(verify), executor.submit(verify)]]

    assert sum("/login" in location for _status, location in results) == 1
    db_session.expire_all()
    assert app_module.OTPCode.query.get(rec.id).is_used is True


def test_concurrent_failed_attempts_do_not_bypass_limit(app_module, db_session, normal_user):
    app_module.OTP_MAX_VERIFY_ATTEMPTS = 2
    _code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    email = normal_user.email
    challenge_id = rec.challenge_id
    barrier = Barrier(2)

    def fail_once(value):
        with app_module.app.test_request_context("/"):
            barrier.wait()
            return app_module._verify_otp(email, "verify_email", value, challenge_id=challenge_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(fail_once, "100000"), executor.submit(fail_once, "200000")]]

    assert results == [False, False]
    db_session.expire_all()
    locked = app_module.OTPCode.query.get(rec.id)
    assert locked.attempt_count <= 2
    assert locked.invalidated_at is not None


def test_correct_otp_racing_final_wrong_attempt_has_safe_outcome(app_module, db_session, normal_user):
    app_module.OTP_MAX_VERIFY_ATTEMPTS = 1
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)
    email = normal_user.email
    challenge_id = rec.challenge_id
    barrier = Barrier(2)

    def submit(value):
        with app_module.app.test_request_context("/"):
            barrier.wait()
            return app_module._verify_otp(email, "verify_email", value, challenge_id=challenge_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [
            executor.submit(submit, code),
            executor.submit(submit, "000000"),
        ]]

    assert results.count(True) <= 1
    db_session.expire_all()
    rec_after = app_module.OTPCode.query.get(rec.id)
    assert rec_after.is_used or rec_after.invalidated_at is not None
    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(email, "verify_email", code, challenge_id=challenge_id) is False


def test_ip_throttle_does_not_permanently_lock_account(app_module, db_session, normal_user):
    app_module.OTP_IP_ATTEMPT_LIMIT = 0
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)

    with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.10"}):
        assert app_module._verify_otp(normal_user.email, "verify_email", code, challenge_id=rec.challenge_id) is False

    db_session.expire_all()
    throttled = app_module.OTPCode.query.get(rec.id)
    assert throttled.locked_until is None
    assert throttled.invalidated_at is None


def test_otp_values_do_not_appear_in_logs(app_module, normal_user, caplog):
    code, rec = _issue_otp(app_module, normal_user.email, user_id=normal_user.id)

    with app_module.app.test_request_context("/"):
        app_module._verify_otp(normal_user.email, "verify_email", "000000", challenge_id=rec.challenge_id)

    assert code not in caplog.text


def test_legacy_plaintext_otp_success_expiry_invalidation_and_no_logging(app_module, db_session, normal_user, caplog):
    legacy = app_module.OTPCode(
        email=normal_user.email,
        code="654321",
        purpose="verify_email",
        expires_at=datetime.utcnow() + timedelta(minutes=2),
    )
    expired = app_module.OTPCode(
        email=normal_user.email,
        code="111111",
        purpose="reset_password",
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    db_session.add_all([legacy, expired])
    db_session.commit()

    with app_module.app.test_request_context("/"):
        assert app_module._verify_otp(normal_user.email, "reset_password", "111111") is False
        assert app_module._verify_otp(normal_user.email, "verify_email", "654321") is True

    assert app_module.OTPCode.query.filter_by(email=normal_user.email, code="654321").first() is None
    assert "654321" not in caplog.text
    assert "111111" not in caplog.text


def test_new_otp_issuance_invalidates_legacy_plaintext_challenge(app_module, db_session, normal_user):
    legacy = app_module.OTPCode(
        email=normal_user.email,
        code="333333",
        purpose="verify_email",
        expires_at=datetime.utcnow() + timedelta(minutes=2),
    )
    db_session.add(legacy)
    db_session.commit()

    code, rec = _issue_otp(app_module, normal_user.email, "verify_email", user_id=normal_user.id)

    db_session.expire_all()
    assert app_module.OTPCode.query.get(legacy.id).invalidated_at is not None
    assert app_module.OTPCode.query.get(rec.id).code == ""
    assert app_module.OTPCode.query.get(rec.id).code_hash
    assert check_password_hash(app_module.OTPCode.query.get(rec.id).code_hash, code)


def test_otp_security_schema_migration_updates_existing_database(app_module, db_session, normal_user):
    _recreate_legacy_otp_table(app_module, db_session)
    db_session.execute(app_module.text(
        "INSERT INTO otp_codes (email, code, purpose, expires_at, is_used, user_id) "
        "VALUES (:email, :code, :purpose, :expires_at, 0, :user_id)"
    ), {
        "email": normal_user.email,
        "code": "123456",
        "purpose": "verify_email",
        "expires_at": datetime.utcnow() + timedelta(minutes=2),
        "user_id": normal_user.id,
    })
    db_session.commit()

    dry_run = app_module.app.test_cli_runner().invoke(args=["migrate-otp-security-schema"])

    assert dry_run.exit_code == 0
    assert "Mode: dry-run" in dry_run.output
    inspector = app_module.inspect(app_module.db.engine)
    columns_before = {column["name"] for column in inspector.get_columns("otp_codes")}
    assert "code_hash" not in columns_before

    result = app_module.app.test_cli_runner().invoke(args=["migrate-otp-security-schema", "--apply"])

    assert result.exit_code == 0
    assert "Mode: apply" in result.output
    inspector = app_module.inspect(app_module.db.engine)
    columns = {column["name"] for column in inspector.get_columns("otp_codes")}
    assert {"code_hash", "challenge_id", "attempt_count", "resend_count", "locked_until", "invalidated_at"} <= columns
    assert app_module.scan_otp_challenge_index_exists() is True
    assert app_module.OTPCode.query.filter_by(email=normal_user.email, code="123456").count() == 1

    second = app_module.app.test_cli_runner().invoke(args=["migrate-otp-security-schema", "--apply"])
    assert second.exit_code == 0


def test_otp_security_migration_refuses_duplicate_non_null_challenge_ids(app_module, db_session, normal_user):
    _recreate_legacy_otp_table(app_module, db_session)
    db_session.execute(app_module.text("ALTER TABLE otp_codes ADD COLUMN challenge_id VARCHAR(64)"))
    for code in ("100000", "200000"):
        db_session.execute(app_module.text(
            "INSERT INTO otp_codes (email, code, purpose, expires_at, is_used, user_id, challenge_id) "
            "VALUES (:email, :code, :purpose, :expires_at, 0, :user_id, :challenge_id)"
        ), {
            "email": normal_user.email,
            "code": code,
            "purpose": "verify_email",
            "expires_at": datetime.utcnow() + timedelta(minutes=2),
            "user_id": normal_user.id,
            "challenge_id": "duplicate-challenge",
        })
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["migrate-otp-security-schema", "--apply"])

    assert result.exit_code != 0
    assert "Duplicate non-null OTP challenge_id values exist" in result.output
    inspector = app_module.inspect(app_module.db.engine)
    columns = {column["name"] for column in inspector.get_columns("otp_codes")}
    assert "code_hash" not in columns


def test_otp_config_validation_rejects_unsafe_values(app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS", "0")
    with pytest.raises(RuntimeError, match="SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS"):
        app_module._otp_int_config("SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS", 5)

    monkeypatch.setenv("SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS", "not-a-number")
    with pytest.raises(RuntimeError, match="SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS"):
        app_module._otp_int_config("SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS", 5)


def test_otp_state_is_not_shared_between_browser_sessions(client, app_module, normal_user):
    code, rec = _issue_otp(app_module, normal_user.email, "verify_email", user_id=normal_user.id)
    other_client = app_module.app.test_client()
    _set_verify_session(client, normal_user.email, rec.challenge_id)

    without_session = other_client.post("/verify-email/", data={"otp": code}, follow_redirects=False)
    with_session = client.post("/verify-email/", data={"otp": code}, follow_redirects=False)

    assert without_session.status_code == 302
    assert "/register" in without_session.headers["Location"]
    assert with_session.status_code == 302
    assert "/login" in with_session.headers["Location"]


def test_logout_clears_pending_otp_state(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["pending_verify_email"] = "user@example.com"
        sess["pending_verify_challenge_id"] = "challenge"
        sess["pending_reset_email"] = "user@example.com"
        sess["pending_reset_challenge_id"] = "reset"

    client.get("/logout/")

    with client.session_transaction() as sess:
        assert "pending_verify_email" not in sess
        assert "pending_reset_email" not in sess


def test_admin_logout_clears_pending_admin_otp_state(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
        sess["pending_admin_reset_email"] = admin.email
        sess["pending_admin_reset_challenge_id"] = "admin-reset"

    client.get("/admin/logout")

    with client.session_transaction() as sess:
        assert "pending_admin_reset_email" not in sess
