"""V1.1 P1 backend security / operations hardening.

Focused coverage for the ten P1 items only. Nothing here touches scanner
recognition, and nothing here re-tests P0 behaviour that already has its own
suite (tests/integration/test_v11_p0_refund_recovery.py).
"""
import json
import smtplib
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

import processing_queue


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _user(app_module, db_session, email, account_type="INDIVIDUAL"):
    user = app_module.User(
        email=email,
        first_name="P1",
        last_name="User",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        account_type=account_type,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _project(app_module, db_session, owner, name="P1 Project", **kwargs):
    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=1,
        is_active=True,
        **kwargs,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _login(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


class _FakeSMTP:
    """Records what would have gone on the wire. Never connects."""

    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        pass

    def sendmail(self, mail_from, to_email, message):
        type(self).sent.append({"from": mail_from, "to": to_email, "message": message})


@pytest.fixture()
def smtp_configured(app_module, monkeypatch):
    _FakeSMTP.sent = []
    for key, value in (
        ("SMTP_HOST", "smtp.example.com"),
        ("SMTP_PORT", "587"),
        ("SMTP_USER", "smtp-user"),
        ("SMTP_PASS", "smtp-pass"),
        ("MAIL_FROM", "no-reply@example.com"),
    ):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


# ===========================================================================
# P1-1 - SMTP header / subject injection
# ===========================================================================
@pytest.mark.parametrize("payload", [
    "Attacker\r\nBcc: victim@example.com",
    "Attacker\nCc: victim@example.com",
    "Attacker\rX-Custom: injected",
    "Attacker\x00null",
])
def test_p1_1_header_helper_rejects_every_line_break_form(app_module, payload):
    with pytest.raises(ValueError):
        app_module.safe_email_header(payload, "subject")


def test_p1_1_header_helper_preserves_unicode(app_module):
    assert app_module.safe_email_header("Zoë Müller — 東京") == "Zoë Müller — 東京"


def test_p1_1_send_email_refuses_injected_subject_before_any_send(app_module, smtp_configured):
    with pytest.raises(ValueError):
        app_module._real_send_email_smtp(
            "ops@example.com", "Hello\r\nBcc: victim@example.com", "<p>body</p>"
        )
    assert smtp_configured.sent == []


def test_p1_1_send_email_refuses_injected_recipient(app_module, smtp_configured):
    with pytest.raises(ValueError):
        app_module._real_send_email_smtp("ops@example.com\r\nBcc: victim@example.com", "Subject", "<p>b</p>")
    assert smtp_configured.sent == []


def test_p1_1_legitimate_email_still_sends_with_unicode_subject_and_body_newlines(app_module, smtp_configured):
    app_module._real_send_email_smtp(
        "ops@example.com", "ScanStory — Grüße", "<p>line one</p>\n<p>line two</p>"
    )

    assert len(smtp_configured.sent) == 1
    message = smtp_configured.sent[0]["message"]
    # RFC 2047 encoded rather than dropped, and exactly one Subject header.
    assert message.count("\nSubject:") + message.startswith("Subject:") == 1
    assert "=?utf-8?" in message
    assert "Bcc:" not in message
    # Body newlines survive: this was never a header concern.
    assert "line one" in message and "line two" in message


def test_p1_1_contact_form_crlf_in_name_is_rejected_with_400(client, app_module, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email_smtp", lambda *a, **k: sent.append(a))

    response = client.post("/send-contact-email", data={
        "name": "Attacker\r\nBcc: victim@example.com",
        "phone": "123",
        "email": "a@b.com",
        "message": "hello",
        "enquiry_type": "support",
    })

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert sent == []


def test_p1_1_contact_form_crlf_in_enquiry_type_is_rejected(client, app_module, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email_smtp", lambda *a, **k: sent.append(a))

    response = client.post("/send-contact-email", data={
        "name": "Normal Name",
        "phone": "123",
        "email": "a@b.com",
        "message": "hello",
        "enquiry_type": "custom\r\nBcc: victim@example.com",
    })

    assert response.status_code == 400
    assert sent == []


def test_p1_1_contact_form_accepts_unicode_name_and_escapes_body(client, app_module, monkeypatch):
    captured = {}

    def fake_send(to_email, subject, html_body):
        captured.update({"to": to_email, "subject": subject, "html": html_body})

    monkeypatch.setattr(app_module, "send_email_smtp", fake_send)

    response = client.post("/send-contact-email", data={
        "name": "Zoë <script>alert(1)</script>",
        "phone": "123",
        "email": "a@b.com",
        "message": "line one\nline two",
        "enquiry_type": "support",
    })

    assert response.status_code == 200
    assert "Zoë" in captured["subject"]
    assert "<script>" not in captured["html"]
    assert "&lt;script&gt;" in captured["html"]
    # Body newlines are still ordinary content.
    assert "line one" in captured["html"]


def test_p1_1_contact_form_failure_does_not_leak_internal_error(client, app_module, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("smtp.internal.example.com refused AUTH for smtp-user")

    monkeypatch.setattr(app_module, "send_email_smtp", boom)

    response = client.post("/send-contact-email", data={
        "name": "Normal", "phone": "1", "email": "a@b.com", "message": "hi", "enquiry_type": "support",
    })

    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "smtp.internal.example.com" not in body
    assert "smtp-user" not in body


# ===========================================================================
# P1-2 - production reCAPTCHA policy
# ===========================================================================
def _unpatched_verify(app_module, action):
    """The real verifier, stashed by conftest before it installs its stub."""
    return app_module._real_verify_recaptcha_v3(action)


def test_p1_2_dev_bypass_remains_supported(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RECAPTCHA_SITE_KEY", "")
    monkeypatch.setattr(app_module, "RECAPTCHA_SECRET_KEY", "")
    for key in ("SCANSTORY_PRODUCTION", "APP_ENV", "ENV", "FLASK_ENV"):
        monkeypatch.delenv(key, raising=False)

    with app_module.app.test_request_context("/", method="POST"):
        ok, message = _unpatched_verify(app_module, "contact")

    assert ok is True
    assert message == "OK"


def test_p1_2_production_without_keys_fails_closed(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RECAPTCHA_SITE_KEY", "")
    monkeypatch.setattr(app_module, "RECAPTCHA_SECRET_KEY", "")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")

    with app_module.app.test_request_context("/", method="POST"):
        ok, message = _unpatched_verify(app_module, "contact")

    assert ok is False
    assert "unavailable" in message.lower()


def test_p1_2_configured_captcha_path_still_succeeds(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RECAPTCHA_SITE_KEY", "site")
    monkeypatch.setattr(app_module, "RECAPTCHA_SECRET_KEY", "secret")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")

    class _Response:
        @staticmethod
        def json():
            return {"success": True, "score": 0.9, "action": "contact", "hostname": "myscanstory.com"}

    monkeypatch.setattr(app_module.requests, "post", lambda *a, **k: _Response())

    with app_module.app.test_request_context(
        "/", method="POST", data={"g-recaptcha-response": "token"}
    ):
        ok, message = _unpatched_verify(app_module, "contact")

    assert (ok, message) == (True, "OK")


def test_p1_2_provider_error_is_not_automatic_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RECAPTCHA_SITE_KEY", "site")
    monkeypatch.setattr(app_module, "RECAPTCHA_SECRET_KEY", "secret")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_module.requests, "post", boom)

    with app_module.app.test_request_context(
        "/", method="POST", data={"g-recaptcha-response": "token"}
    ):
        ok, _message = _unpatched_verify(app_module, "contact")

    assert ok is False


def test_p1_2_no_key_value_is_ever_returned_to_the_caller(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RECAPTCHA_SITE_KEY", "super-secret-site-key")
    monkeypatch.setattr(app_module, "RECAPTCHA_SECRET_KEY", "")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")

    with app_module.app.test_request_context("/", method="POST"):
        _ok, message = _unpatched_verify(app_module, "contact")

    assert "super-secret-site-key" not in message


# ===========================================================================
# P1-3 - worker-aware /ready
# ===========================================================================
class _FakeWorker:
    def __init__(self, last_heartbeat=None, death_date=None):
        self.last_heartbeat = last_heartbeat
        self.death_date = death_date


def test_p1_3_non_rq_modes_are_not_applicable_not_failures(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    assert processing_queue.queue_worker_state() == ("not_applicable", 0)
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "inline")
    assert processing_queue.queue_worker_state() == ("not_applicable", 0)


def test_p1_3_rq_without_redis_url_is_unavailable(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert processing_queue.queue_worker_state() == ("unavailable", 0)


def test_p1_3_zero_workers_is_unavailable(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(processing_queue, "_rq_workers_for_queue", lambda: [])
    assert processing_queue.queue_worker_state() == ("unavailable", 0)


def test_p1_3_live_worker_is_ok(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(
        processing_queue, "_rq_workers_for_queue",
        lambda: [_FakeWorker(last_heartbeat=datetime.utcnow())],
    )
    assert processing_queue.queue_worker_state() == ("ok", 1)


def test_p1_3_stale_heartbeat_worker_is_not_counted(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("RQ_WORKER_STALE_AFTER_SECONDS", "60")
    monkeypatch.setattr(
        processing_queue, "_rq_workers_for_queue",
        lambda: [_FakeWorker(last_heartbeat=datetime.utcnow() - timedelta(hours=3))],
    )
    assert processing_queue.queue_worker_state() == ("unavailable", 0)


def test_p1_3_dead_worker_is_not_counted(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(
        processing_queue, "_rq_workers_for_queue",
        lambda: [_FakeWorker(last_heartbeat=datetime.utcnow(), death_date=datetime.utcnow())],
    )
    assert processing_queue.queue_worker_state() == ("unavailable", 0)


def test_p1_3_redis_probe_failure_is_unavailable(monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    def boom():
        raise RuntimeError("redis gone")

    monkeypatch.setattr(processing_queue, "_rq_workers_for_queue", boom)
    assert processing_queue.queue_worker_state() == ("unavailable", 0)


def test_p1_3_ready_is_503_when_redis_is_up_but_no_worker_exists(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: True)
    monkeypatch.setattr(app_module, "queue_worker_state", lambda: ("unavailable", 0))

    response = client.get("/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["queue"] == "ok"
    assert payload["checks"]["workers"] == "unavailable"
    assert payload["checks"]["usable_worker_count"] == 0
    # No worker identity or connection string anywhere in the response.
    body = response.get_data(as_text=True)
    assert "redis://" not in body and "127.0.0.1" not in body


def test_p1_3_healthz_stays_lightweight_and_ignores_workers(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    def worker_check_should_not_run():
        raise AssertionError("/healthz must not perform queue diagnostics")

    monkeypatch.setattr(app_module, "queue_worker_state", worker_check_should_not_run)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


# ===========================================================================
# P1-4 - transfer expiry
# ===========================================================================
def _transfer(app_module, db_session, sender, recipient, project):
    transfer = app_module.initiate_project_ownership_transfer(project, sender, recipient)
    db_session.commit()
    return transfer


def test_p1_4_new_transfer_receives_a_deadline(app_module, db_session):
    sender = _user(app_module, db_session, "p14-sender@example.com")
    recipient = _user(app_module, db_session, "p14-recipient@example.com")
    project = _project(app_module, db_session, sender)

    transfer = _transfer(app_module, db_session, sender, recipient, project)

    assert transfer.expires_at is not None
    expected = datetime.utcnow() + timedelta(days=app_module.ownership_transfer_expiry_days())
    assert abs((transfer.expires_at - expected).total_seconds()) < 120


def test_p1_4_expired_transfer_cannot_be_accepted_and_ownership_never_moves(app_module, db_session):
    sender = _user(app_module, db_session, "p14b-sender@example.com")
    recipient = _user(app_module, db_session, "p14b-recipient@example.com")
    project = _project(app_module, db_session, sender)
    transfer = _transfer(app_module, db_session, sender, recipient, project)

    transfer.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    with pytest.raises(ValueError):
        app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()

    assert transfer.status == "EXPIRED"
    assert app_module.project_current_owner_user_id(project) == sender.id


def test_p1_4_expiry_helper_is_idempotent(app_module, db_session):
    sender = _user(app_module, db_session, "p14c-sender@example.com")
    recipient = _user(app_module, db_session, "p14c-recipient@example.com")
    project = _project(app_module, db_session, sender)
    transfer = _transfer(app_module, db_session, sender, recipient, project)
    transfer.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    assert app_module.expire_transfer_if_due(transfer) is True
    db_session.commit()
    trail_length = len(app_module.ownership_audit_trail(transfer))

    # Second pass: still True, no second transition, no second audit entry.
    assert app_module.expire_transfer_if_due(transfer) is True
    db_session.commit()
    assert transfer.status == "EXPIRED"
    assert len(app_module.ownership_audit_trail(transfer)) == trail_length


def test_p1_4_non_expired_transfer_is_untouched(app_module, db_session):
    sender = _user(app_module, db_session, "p14d-sender@example.com")
    recipient = _user(app_module, db_session, "p14d-recipient@example.com")
    project = _project(app_module, db_session, sender)
    transfer = _transfer(app_module, db_session, sender, recipient, project)

    assert app_module.expire_transfer_if_due(transfer) is False
    assert transfer.status == "PENDING_ACCEPTANCE"


def test_p1_4_completed_transfer_is_never_reopened_by_a_deadline(app_module, db_session):
    sender = _user(app_module, db_session, "p14e-sender@example.com")
    recipient = _user(app_module, db_session, "p14e-recipient@example.com")
    project = _project(app_module, db_session, sender)
    transfer = _transfer(app_module, db_session, sender, recipient, project)
    transfer.status = "COMPLETED"
    transfer.expires_at = datetime.utcnow() - timedelta(days=5)
    db_session.commit()

    assert app_module.expire_transfer_if_due(transfer) is False
    assert transfer.status == "COMPLETED"


def test_p1_4_cli_expires_stale_transfers_and_leaves_claims_alone(app_module, db_session):
    sender = _user(app_module, db_session, "p14f-sender@example.com")
    recipient = _user(app_module, db_session, "p14f-recipient@example.com")
    project = _project(app_module, db_session, sender)
    transfer = _transfer(app_module, db_session, sender, recipient, project)
    claim = app_module.create_project_ownership_claim(project, recipient)
    claim.transfer_id = transfer.id
    transfer.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()
    claim_status_before = claim.status

    runner = app_module.app.test_cli_runner()
    dry = runner.invoke(args=["expire-ownership-transfers"])
    assert dry.exit_code == 0
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "PENDING_ACCEPTANCE"

    applied = runner.invoke(args=["expire-ownership-transfers", "--apply"])
    assert applied.exit_code == 0
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "EXPIRED"
    # The linked claim is a separate lifecycle and is NOT cancelled.
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == claim_status_before

    rerun = runner.invoke(args=["expire-ownership-transfers", "--apply"])
    assert rerun.exit_code == 0


# ===========================================================================
# P1-5 - vendor-before-admin claim governance
# ===========================================================================
def _vendor_managed_claim(app_module, db_session):
    owner = _user(app_module, db_session, "p15-owner@example.com")
    vendor = _user(app_module, db_session, "p15-vendor@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, "p15-claimant@example.com")
    project = _project(app_module, db_session, owner, manager_vendor_user_id=vendor.id)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()
    return owner, vendor, claimant, project, claim


def test_p1_5_vendor_managed_open_claim_cannot_be_approved_by_admin(app_module, db_session, admin):
    _owner, _vendor, _claimant, _project, claim = _vendor_managed_claim(app_module, db_session)

    assert claim.status == "OPEN"
    with pytest.raises(PermissionError):
        app_module.approve_project_ownership_claim_by_admin(claim, admin, "premature")
    db_session.rollback()
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == "OPEN"


def test_p1_5_vendor_managed_open_claim_cannot_be_rejected_by_admin(app_module, db_session, admin):
    _owner, _vendor, _claimant, _project, claim = _vendor_managed_claim(app_module, db_session)

    with pytest.raises(PermissionError):
        app_module.reject_project_ownership_claim_by_admin(claim, admin, "premature")
    db_session.rollback()
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == "OPEN"


def test_p1_5_admin_review_works_after_vendor_refusal(app_module, db_session, admin):
    _owner, vendor, claimant, project, claim = _vendor_managed_claim(app_module, db_session)

    claim, transfer = app_module.respond_to_project_ownership_claim(claim, vendor, accept=False)
    db_session.commit()
    assert claim.status == "PENDING_ADMIN_REVIEW"
    assert transfer is None

    claim, transfer = app_module.approve_project_ownership_claim_by_admin(claim, admin, "verified")
    db_session.commit()

    assert claim.status == "APPROVED_BY_ADMIN"
    # Approval opens a transfer; it does NOT move ownership.
    assert transfer is not None and transfer.status == "PENDING_ACCEPTANCE"
    assert app_module.project_current_owner_user_id(project) != claimant.id


def test_p1_5_admin_review_unblocks_after_the_vendor_response_deadline(app_module, db_session, admin):
    _owner, _vendor, _claimant, _project, claim = _vendor_managed_claim(app_module, db_session)

    assert claim.response_deadline_at is not None
    assert app_module.claim_admin_review_block_reason(claim) is not None

    claim.response_deadline_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    assert app_module.claim_admin_review_block_reason(claim) is None
    claim, _transfer = app_module.approve_project_ownership_claim_by_admin(claim, admin, "vendor silent")
    db_session.commit()
    assert claim.status == "APPROVED_BY_ADMIN"


def test_p1_5_project_without_managing_vendor_keeps_the_direct_admin_path(app_module, db_session, admin):
    owner = _user(app_module, db_session, "p15b-owner@example.com")
    claimant = _user(app_module, db_session, "p15b-claimant@example.com")
    project = _project(app_module, db_session, owner)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()

    assert claim.status == "OPEN"
    assert app_module.claim_admin_review_block_reason(claim) is None

    claim, transfer = app_module.approve_project_ownership_claim_by_admin(claim, admin, "no vendor")
    db_session.commit()

    assert claim.status == "APPROVED_BY_ADMIN"
    assert claim.reviewed_by_admin_id == admin.id
    assert transfer is not None
    assert app_module.project_current_owner_user_id(project) == owner.id


def test_p1_5_direct_admin_rejection_stays_auditable(app_module, db_session, admin):
    owner = _user(app_module, db_session, "p15c-owner@example.com")
    claimant = _user(app_module, db_session, "p15c-claimant@example.com")
    project = _project(app_module, db_session, owner)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()

    app_module.reject_project_ownership_claim_by_admin(claim, admin, "insufficient evidence")
    db_session.commit()

    assert claim.status == "REJECTED"
    assert claim.reviewed_by_admin_id == admin.id
    actions = [entry["action"] for entry in app_module.ownership_audit_trail(claim)]
    assert "claim_rejected_by_admin" in actions
    assert app_module.project_current_owner_user_id(project) == owner.id


# ===========================================================================
# P1-6 / P1-7 - refund attention queue and out-of-band correlation
# ===========================================================================
def _paid_plan(app_module, db_session, name):
    plan = app_module.SubscriptionPlan(
        plan_name=name,
        plan_amount=100.0,
        currency="INR",
        duration_type="time",
        duration_value=12,
        total_project_limit=5,
        total_scan_limit=500,
        max_pairs_per_project=10,
        is_active=True,
    )
    db_session.add(plan)
    db_session.commit()
    return plan


def _order_and_refund(app_module, db_session, admin, user, suffix, **overrides):
    plan = _paid_plan(app_module, db_session, f"P1 Plan {suffix}")
    order = app_module.PaymentOrder(
        order_id=f"ORDER_P1_{suffix}",
        user_id=user.id,
        plan_id=plan.id,
        amount=100.0,
        total_amount=100.0,
        currency="INR",
        status="success",
        razorpay_payment_id=f"pay_p1_{suffix}",
    )
    db_session.add(order)
    db_session.commit()
    refund = app_module.PaymentRefund(
        payment_order_id=order.id,
        user_id=user.id,
        provider_payment_id=order.razorpay_payment_id,
        amount=order.total_amount,
        currency="INR",
        status=overrides.get("status", "REFUNDED"),
        reconciliation_status=overrides.get("reconciliation_status", "APPLIED"),
        reason="p1 operational test",
        requested_by_admin_id=admin.id,
        idempotency_key=f"refund:payment_order:{order.id}",
    )
    db_session.add(refund)
    db_session.commit()
    return order, refund


def test_p1_6_attention_queue_matches_the_reconcile_command_definition(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "p16@example.com")
    settled = _order_and_refund(app_module, db_session, admin, user, "settled")[1]
    failed = _order_and_refund(app_module, db_session, admin, user, "failed", status="REFUND_FAILED",
                               reconciliation_status="PENDING")[1]
    processing = _order_and_refund(app_module, db_session, admin, user, "processing",
                                   status="REFUND_PROCESSING", reconciliation_status="PENDING")[1]
    recon_failed = _order_and_refund(app_module, db_session, admin, user, "reconfail",
                                     reconciliation_status="FAILED")[1]
    manual = _order_and_refund(app_module, db_session, admin, user, "manual",
                               reconciliation_status="MANUAL_REVIEW_REQUIRED")[1]

    payload = client.get("/admin/api/refunds?needs_attention=1").get_json()
    ids = {row["id"] for row in payload["refunds"]}

    assert {failed.id, processing.id, recon_failed.id, manual.id} <= ids
    assert settled.id not in ids
    # The API and the CLI now work from ONE predicate.
    assert ids == {row.id for row in app_module.stuck_refund_query().all()}
    # Manual-review reason/state is readable from the contract itself.
    manual_row = next(row for row in payload["refunds"] if row["id"] == manual.id)
    assert manual_row["reconciliation_status"] == "MANUAL_REVIEW_REQUIRED"
    assert "reconciliation_message_safe" in manual_row


def test_p1_6_attention_queue_exposes_out_of_band_refunds_without_provider_payload(
    client, app_module, db_session, admin
):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "p16b@example.com")
    order, _refund = _order_and_refund(app_module, db_session, admin, user, "oob")
    event = app_module.RazorpayWebhookEvent(
        idempotency_key="refund.processed|rfnd_oob|pay_p1_oob",
        event_type="refund.processed",
        razorpay_payment_id="pay_p1_oob",
        payload_hash="0" * 64,
        processing_status="failed",
        failure_code=app_module.OUT_OF_BAND_REFUND_FAILURE_CODE,
        payment_order_id=order.id,
    )
    db_session.add(event)
    db_session.commit()

    payload = client.get("/admin/api/refunds?needs_attention=1").get_json()

    assert payload["out_of_band_total"] == 1
    entry = payload["out_of_band_refunds"][0]
    assert entry["webhook_event_id"] == event.id
    assert entry["payment_order_id"] == order.id
    assert entry["state"] == "MANUAL_REVIEW_REQUIRED"
    assert "payload" not in entry and "signature" not in entry


def test_p1_6_recover_endpoint_uses_the_existing_recovery_helper(client, app_module, db_session, admin, monkeypatch):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "p16c@example.com")
    _order, refund = _order_and_refund(app_module, db_session, admin, user, "recover",
                                       reconciliation_status="MANUAL_REVIEW_REQUIRED")

    calls = []
    real = app_module.recover_payment_refund

    def spy(target, admin=None, apply_changes=False):
        calls.append((target.id, apply_changes))
        return real(target, admin=admin, apply_changes=apply_changes)

    monkeypatch.setattr(app_module, "recover_payment_refund", spy)

    response = client.post(f"/admin/api/refunds/{refund.id}/recover", json={"apply": True})

    assert calls == [(refund.id, True)]
    # Manual review is never auto-resolved.
    assert response.status_code == 409
    assert response.get_json()["recovery"]["outcome"] == "manual_review"
    db_session.expire_all()
    assert app_module.PaymentRefund.query.get(refund.id).reconciliation_status == "MANUAL_REVIEW_REQUIRED"


def test_p1_7_out_of_band_refund_is_correlated_not_guessed(app_module, db_session, admin):
    """P0 built this; this is the focused proof it behaves per the invariant."""
    user = _user(app_module, db_session, "p17@example.com")
    order, refund = _order_and_refund(app_module, db_session, admin, user, "p17")
    # No local refund record for THIS provider refund id / payment.
    refund.provider_refund_id = "rfnd_other"
    refund.status = "REFUNDED"
    refund.provider_payment_id = "pay_unrelated"
    db_session.commit()

    event = app_module.RazorpayWebhookEvent(
        idempotency_key="refund.processed|rfnd_dashboard|pay_p1_p17",
        event_type="refund.processed",
        razorpay_payment_id=order.razorpay_payment_id,
        payload_hash="1" * 64,
        processing_status="received",
    )
    db_session.add(event)
    db_session.commit()

    handled = app_module._process_refund_webhook_event(
        event,
        {"id": "rfnd_dashboard", "payment_id": order.razorpay_payment_id, "status": "processed",
         "amount": 10000, "currency": "INR"},
        {},
    )
    db_session.commit()

    assert handled is True
    assert event.failure_code == app_module.OUT_OF_BAND_REFUND_FAILURE_CODE
    assert event.payment_order_id == order.id
    # No local refund record was fabricated for it.
    assert app_module.PaymentRefund.query.filter_by(provider_refund_id="rfnd_dashboard").first() is None
    assert event.id in {e.id for e in app_module.unlinked_out_of_band_refund_events()}


def test_p1_7_uncorrelatable_provider_refund_is_not_attributed_to_a_purchase(app_module, db_session):
    event = app_module.RazorpayWebhookEvent(
        idempotency_key="refund.processed|rfnd_unknown|pay_nothing",
        event_type="refund.processed",
        razorpay_payment_id="pay_nothing",
        payload_hash="2" * 64,
        processing_status="received",
    )
    app_module.db.session.add(event)
    app_module.db.session.commit()

    app_module._process_refund_webhook_event(
        event,
        {"id": "rfnd_unknown", "payment_id": "pay_nothing", "status": "processed",
         "amount": 10000, "currency": "INR"},
        {},
    )
    app_module.db.session.commit()

    assert event.failure_code == "unknown_refund"
    assert event.payment_order_id is None
    assert event.addon_purchase_id is None


# ===========================================================================
# P1-8 - storage reconciliation operational hardening
# ===========================================================================
def test_p1_8_json_output_is_valid_json_and_secret_free(app_module, project_with_pair):
    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["reconcile-storage", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert set(payload["counts"]) == {
        "missing_files", "orphan_files", "ambiguous_ownership",
        "size_mismatches", "counter_drift", "errors",
    }
    lowered = result.output.lower()
    assert "password" not in lowered and "secret" not in lowered
    assert "sqlite:///" not in lowered


def test_p1_8_dry_run_writes_nothing(app_module, project_with_pair):
    before = app_module.MediaObject.query.count()
    result = app_module.app.test_cli_runner().invoke(args=["reconcile-storage"])
    assert result.exit_code == 0
    assert app_module.MediaObject.query.count() == before


def test_p1_8_ambiguous_ownership_produces_a_nonzero_exit(app_module, db_session):
    orphan_project = app_module.Project(name="No Owner", user_project_index=1, is_active=True)
    db_session.add(orphan_project)
    db_session.commit()
    db_session.add(app_module.ProjectPair(
        project_id=orphan_project.id, pair_index=0,
        image_filename="x.jpg", video_filename="x.mp4",
    ))
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-storage", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["counts"]["ambiguous_ownership"] >= 1
    assert payload["needs_human_total"] >= 1


def test_p1_8_orphan_file_is_reported_and_left_on_disk(app_module, db_session, tmp_path):
    from pathlib import Path

    orphan = Path(app_module.IMAGES_DIR) / "999999_0.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan bytes")

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-storage", "--apply", "--json"])

    payload = json.loads(result.output)
    assert payload["counts"]["orphan_files"] >= 1
    # An orphan is a report, never a deletion, and never a hard failure.
    assert orphan.exists()
    assert orphan.read_bytes() == b"orphan bytes"
    assert result.exit_code == 0


def test_p1_8_apply_is_idempotent(app_module, project_with_pair):
    runner = app_module.app.test_cli_runner()
    first = runner.invoke(args=["reconcile-storage", "--apply", "--json"])
    assert first.exit_code == 0
    created_first = json.loads(first.output)["created"]

    second = runner.invoke(args=["reconcile-storage", "--apply", "--json"])
    assert second.exit_code == 0
    payload = json.loads(second.output)
    assert payload["created"] == 0
    assert payload["already_reconciled"] >= created_first


def test_p1_8_dry_run_remains_the_default(app_module):
    command = app_module.app.cli.get_command(None, "reconcile-storage")
    apply_option = next(p for p in command.params if p.name == "apply_changes")
    assert apply_option.default is False


# ===========================================================================
# P1-9 - project-list coverage summary contract
# ===========================================================================
def _coverage_state(app_module, project):
    return app_module.project_coverage_summary(project)["coverage_state"]


def test_p1_9_active_coverage_summary(app_module, db_session):
    owner = _user(app_module, db_session, "p19-active@example.com")
    owner.subscription_status = "active"
    owner.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    db_session.commit()
    project = _project(app_module, db_session, owner)

    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "active"
    assert summary["is_live"] is True
    assert summary["effective_coverage_until"] is not None
    assert summary["is_suspended"] is False


def test_p1_9_expired_coverage_summary(app_module, db_session):
    owner = _user(app_module, db_session, "p19-expired@example.com")
    owner.subscription_status = "expired"
    owner.subscription_expires_at = datetime.utcnow() - timedelta(days=5)
    db_session.commit()
    project = _project(app_module, db_session, owner)

    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "expired"
    assert summary["is_live"] is False
    assert summary["is_suspended"] is False


def test_p1_9_no_coverage_summary(app_module, db_session):
    owner = _user(app_module, db_session, "p19-none@example.com")
    owner.subscription_status = "none"
    owner.subscription_expires_at = None
    db_session.commit()
    project = _project(app_module, db_session, owner)

    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "none"
    assert summary["is_live"] is False


def test_p1_9_suspended_stays_distinct_from_expired(app_module, db_session):
    owner = _user(app_module, db_session, "p19-susp@example.com")
    owner.subscription_status = "active"
    owner.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    db_session.commit()
    project = _project(app_module, db_session, owner)
    project.is_active = False
    db_session.commit()

    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "suspended"
    assert summary["is_suspended"] is True
    assert summary["coverage_state"] != "expired"


def test_p1_9_project_list_route_attaches_a_summary_per_project(client, app_module, db_session):
    owner = _user(app_module, db_session, "p19-list@example.com")
    owner.subscription_status = "active"
    owner.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    db_session.commit()
    for index in range(3):
        _project(app_module, db_session, owner, name=f"Listed {index}")
    _login(client, owner)

    captured = {}
    real_render = app_module.render_template

    def spy_render(template_name, **context):
        if template_name == "user/projects.html":
            captured["projects"] = context["projects"]
        return real_render(template_name, **context)

    app_module.render_template = spy_render
    try:
        response = client.get("/projects")
    finally:
        app_module.render_template = real_render

    assert response.status_code == 200
    assert len(captured["projects"]) == 3
    for project in captured["projects"]:
        summary = project.coverage_summary
        assert summary["project_id"] == project.id
        assert summary["coverage_state"] == "active"
        assert set(summary) >= {
            "project_id", "coverage_state", "is_live", "reason", "coverage_source",
            "effective_coverage_until", "renewal_starts_at", "renewal_eligible",
            "renewal_blocked_code", "is_suspended",
        }


# ===========================================================================
# P1-10 - claimant discovery / claim submission contract
# ===========================================================================
def _claimable_project(app_module, db_session):
    owner = _user(app_module, db_session, "p110-owner@example.com")
    owner.subscription_status = "active"
    owner.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    db_session.commit()
    project = _project(app_module, db_session, owner, name="Claimable Story")
    return owner, project


def test_p1_10_valid_reference_reaches_the_claim_flow(client, app_module, db_session):
    _owner, project = _claimable_project(app_module, db_session)
    claimant = _user(app_module, db_session, "p110-claimant@example.com")
    _login(client, claimant)

    payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()

    assert payload["eligible"] is True
    assert payload["reason_code"] == "CLAIMABLE"
    assert payload["project"] == {"id": project.id, "name": "Claimable Story"}
    assert payload["claim_url"] == f"/projects/{project.id}/ownership-claim"

    submitted = client.post(payload["claim_url"], data={"evidence_summary": "I printed this"})
    assert submitted.status_code in (302, 303)
    assert app_module.ProjectOwnershipClaim.query.filter_by(
        project_id=project.id, claimant_user_id=claimant.id
    ).count() == 1


def test_p1_10_unknown_reference_is_indistinguishable_from_an_ineligible_one(client, app_module, db_session):
    _owner, project = _claimable_project(app_module, db_session)
    project.is_active = False  # exists, but not publicly available
    db_session.commit()
    claimant = _user(app_module, db_session, "p110-c2@example.com")
    _login(client, claimant)

    missing = client.get("/api/ownership/claim-lookup/99424242").get_json()
    suspended = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()

    assert missing == suspended
    assert missing["project"] is None
    assert missing["eligible"] is False
    assert "Claimable Story" not in json.dumps(missing)


def test_p1_10_owner_and_manager_get_no_claim_path(client, app_module, db_session):
    owner, project = _claimable_project(app_module, db_session)
    _login(client, owner)

    payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()
    assert payload["eligible"] is False
    assert payload["project"] is None


def test_p1_10_duplicate_active_claim_is_reported_not_duplicated(client, app_module, db_session):
    _owner, project = _claimable_project(app_module, db_session)
    claimant = _user(app_module, db_session, "p110-c3@example.com")
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()
    _login(client, claimant)

    payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()

    assert payload["eligible"] is False
    assert payload["reason_code"] == "ALREADY_OPEN"
    assert payload["existing_claim_id"] == claim.id

    client.post(f"/projects/{project.id}/ownership-claim", data={})
    assert app_module.ProjectOwnershipClaim.query.filter_by(
        project_id=project.id, claimant_user_id=claimant.id
    ).count() == 1


def test_p1_10_lookup_requires_login_and_is_read_only(app_module, client, db_session):
    _owner, project = _claimable_project(app_module, db_session)

    anonymous = client.get(f"/api/ownership/claim-lookup/{project.id}")
    assert anonymous.status_code in (302, 303, 401, 403)

    rule = next(
        r for r in app_module.app.url_map.iter_rules()
        if str(r) == "/api/ownership/claim-lookup/<int:project_id>"
    )
    assert rule.methods & {"POST", "PUT", "PATCH", "DELETE"} == set()


def test_p1_10_claim_submission_never_changes_ownership(client, app_module, db_session):
    owner, project = _claimable_project(app_module, db_session)
    claimant = _user(app_module, db_session, "p110-c4@example.com")
    _login(client, claimant)

    client.post(f"/projects/{project.id}/ownership-claim", data={"evidence_summary": "mine"})
    db_session.expire_all()

    assert app_module.project_current_owner_user_id(
        app_module.Project.query.get(project.id)
    ) == owner.id
