"""Razorpay webhook + payment reconciliation tests (v1/razorpay-webhook-reconciliation).

Extends the existing Razorpay mocking approach from
tests/integration/test_payment_idempotency_and_capacity.py, but the webhook
route itself never touches app_module.razorpay_client - signature
verification uses a fresh razorpay.Utility() instance keyed by
RAZORPAY_WEBHOOK_SECRET (see app.py's _razorpay_webhook_signature_valid).
So these tests sign raw request bodies with a real HMAC-SHA256 (the exact
algorithm Razorpay itself uses) against a test secret injected via
monkeypatch - never a real Razorpay credential, never a network call.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from werkzeug.security import generate_password_hash

WEBHOOK_SECRET = "whsec_test_only_never_real"
WEBHOOK_URL = "/webhooks/razorpay"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sign(raw_body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Same algorithm Razorpay documents: HMAC-SHA256(raw_body, secret)."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _payment_captured_payload(order_id, payment_id="pay_wh_1", amount_paise=100000, currency="INR", status="captured", event="payment.captured"):
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": event,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": currency,
                    "status": status,
                    "order_id": order_id,
                }
            }
        },
        "created_at": 1700000000,
    }


def _post_raw(client, raw_body, signature=_sign, secret=WEBHOOK_SECRET, omit_signature=False):
    headers = {"Content-Type": "application/json"}
    if not omit_signature:
        sig = signature(raw_body, secret) if callable(signature) else signature
        headers["X-Razorpay-Signature"] = sig
    return client.post(WEBHOOK_URL, data=raw_body, headers=headers)


def _post_event(client, payload_dict, secret=WEBHOOK_SECRET, omit_signature=False, bad_signature=False):
    raw_body = json.dumps(payload_dict).encode("utf-8")
    if bad_signature:
        return _post_raw(client, raw_body, signature=lambda b, s: "0" * 64, omit_signature=omit_signature)
    return _post_raw(client, raw_body, omit_signature=omit_signature, secret=secret)


def _with_webhook_secret(app_module, monkeypatch, secret=WEBHOOK_SECRET):
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", secret)


def _paid_plan(app_module):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()


def _make_pending_order(app_module, db_session, user, plan, razorpay_order_id, order_id="ORD_WH", amount=None, currency=None):
    order = app_module.PaymentOrder(
        order_id=order_id,
        razorpay_order_id=razorpay_order_id,
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=amount if amount is not None else plan.effective_price,
        currency=currency if currency is not None else plan.currency,
        status="pending",
    )
    db_session.add(order)
    db_session.commit()
    return order


def _reserved_reservation(app_module, db_session, user, order, status="reserved", expires_delta=timedelta(minutes=30)):
    reservation = app_module.PaymentReservation(
        user_id=user.id,
        payment_order_id=order.id,
        status=status,
        expires_at=datetime.utcnow() + expires_delta,
    )
    db_session.add(reservation)
    config = app_module._get_or_create_capacity_config()
    if status in ("reserved", "activated"):
        config.consumed_count = (config.consumed_count or 0) + 1
    db_session.commit()
    return reservation


def _second_user(app_module, db_session, plan, email="second-wh@example.com"):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscription_taken_at=datetime.utcnow(),
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _mock_email(app_module, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "send_payment_success_email", lambda user, plan, order: calls.append(order.id))
    return calls


def _verify_form(order_id, payment_id="pay_browser_1", signature="sig"):
    return {
        "razorpay_payment_id": payment_id,
        "razorpay_order_id": order_id,
        "razorpay_signature": signature,
    }


class FakeRazorpayUtilityOK:
    def verify_payment_signature(self, params):
        return True


class FakeRazorpayClientOK:
    def __init__(self):
        self.utility = FakeRazorpayUtilityOK()


# ---------------------------------------------------------------------------
# 1. Signature / security
# ---------------------------------------------------------------------------
def test_missing_signature_header_rejected(client, app_module, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    response = _post_event(client, _payment_captured_payload("order_x"), omit_signature=True)
    assert response.status_code == 400
    assert response.get_json()["error"] == "missing_signature"


def test_invalid_signature_rejected(client, app_module, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    response = _post_event(client, _payment_captured_payload("order_x"), bad_signature=True)
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_signature"


def test_missing_webhook_secret_fails_closed(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", "")
    raw_body = json.dumps(_payment_captured_payload("order_x")).encode("utf-8")
    # Even a "correctly" signed request (signed with some secret) must be
    # rejected - there is no secret configured to verify against at all.
    response = _post_raw(client, raw_body, signature=lambda b, s: _sign(b, "any-secret"))
    assert response.status_code == 400
    assert response.get_json()["error"] == "webhook_not_configured"
    assert app_module.RazorpayWebhookEvent.query.count() == 0, "nothing should be processed when secret is unset"


def test_valid_signature_with_known_order_is_accepted(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_valid_sig")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_valid_sig", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert app_module.PaymentOrder.query.get(order.id).status == "success"


def test_verification_uses_genuine_raw_bytes_not_reserialized_json(client, app_module, db_session, normal_user, monkeypatch):
    """A raw body with non-canonical formatting (extra whitespace, and a key
    order different from what json.dumps(..., default settings) would
    produce) that is signed over its EXACT bytes must still verify - proving
    the handler HMACs request.get_data() directly and never re-serializes
    parsed JSON before checking the signature (re-serializing would change
    whitespace/key order and break the signature)."""
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_raw_bytes")
    _reserved_reservation(app_module, db_session, normal_user, order)

    amount_paise = round(order.total_amount * 100)
    # Deliberately quirky formatting: reversed top-level key order, extra
    # internal spacing - NOT what json.dumps(payload) would emit by default.
    raw_body = (
        '{"created_at":  1700000000,   "payload": {"payment": {"entity": '
        '{"order_id":   "order_raw_bytes",   "status": "captured",  '
        '"currency": "%s",   "amount":  %d,   "id":   "pay_raw_bytes",  '
        '"entity": "payment"}}},   "contains": ["payment"],  '
        '"event":  "payment.captured",   "account_id":  "acc_test",  '
        '"entity":  "event"}' % (order.currency, amount_paise)
    ).encode("utf-8")

    response = _post_raw(client, raw_body)

    assert response.status_code == 200, response.get_json()
    assert app_module.PaymentOrder.query.get(order.id).status == "success"


def test_malformed_json_after_valid_signature_handled_safely(client, app_module, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    raw_body = b"{not valid json at all"
    response = _post_raw(client, raw_body)
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_payload"


def test_rejection_paths_leak_no_secret_or_payload(client, app_module, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    responses = [
        _post_event(client, _payment_captured_payload("order_leak"), omit_signature=True),
        _post_event(client, _payment_captured_payload("order_leak"), bad_signature=True),
        _post_raw(client, b"{bad json"),
    ]
    for response in responses:
        body_text = response.get_data(as_text=True)
        assert WEBHOOK_SECRET not in body_text
        assert "order_leak" not in body_text
        assert "Traceback" not in body_text


# ---------------------------------------------------------------------------
# 2. Events
# ---------------------------------------------------------------------------
def test_unsupported_event_type_acknowledged_with_zero_mutation(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_unsupported")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_unsupported", event="payment.authorized")
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending", "unsupported event must not mutate anything"
    event = app_module.RazorpayWebhookEvent.query.filter_by(event_type="payment.authorized").first()
    assert event is not None
    assert event.processing_status == "ignored"
    assert event.payment_order_id is None


def test_unknown_razorpay_order_creates_no_entitlement(client, app_module, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    payload = _payment_captured_payload("order_never_existed", amount_paise=50000, currency="INR")
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.filter_by(razorpay_order_id="order_never_existed").count() == 0
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_never_existed").first()
    assert event is not None
    assert event.processing_status == "failed"
    assert event.failure_code == "unknown_order"


def test_missing_plan_blocks_activation(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_no_plan")
    order.plan_id = 999999  # no such SubscriptionPlan
    db_session.commit()

    payload = _payment_captured_payload("order_no_plan", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_no_plan").first()
    assert event.processing_status == "failed"
    assert event.failure_code == "plan_not_found"


def test_amount_mismatch_blocks_activation(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_amt_mismatch")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_amt_mismatch", amount_paise=round(order.total_amount * 100) + 5000, currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_amt_mismatch").first()
    assert event.failure_code == "amount_mismatch"


def test_currency_mismatch_blocks_activation(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_ccy_mismatch")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_ccy_mismatch", amount_paise=round(order.total_amount * 100), currency="USD")
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_ccy_mismatch").first()
    assert event.failure_code == "currency_mismatch"


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------
def test_identical_webhook_delivered_twice_is_noop_second_time(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    email_calls = _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_dup_delivery")
    _reserved_reservation(app_module, db_session, normal_user, order)
    payload = _payment_captured_payload("order_dup_delivery", amount_paise=round(order.total_amount * 100), currency=order.currency)

    first = _post_event(client, payload)
    assert first.status_code == 200
    first_end = app_module.PaymentOrder.query.get(order.id).subscription_end

    second = _post_event(client, payload)  # identical body -> identical idempotency_key
    assert second.status_code == 200
    assert second.get_json().get("replay") is True

    refreshed = app_module.PaymentOrder.query.get(order.id)
    assert refreshed.subscription_end == first_end
    assert len(email_calls) == 1
    events = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_dup_delivery").all()
    assert len(events) == 1, "a true replay must not create a second event row"
    assert events[0].attempt_count == 2


def test_two_concurrent_identical_deliveries_result_in_one_activation(app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_concurrent_wh")
    _reserved_reservation(app_module, db_session, normal_user, order)
    raw_body = json.dumps(
        _payment_captured_payload("order_concurrent_wh", payment_id="pay_concurrent", amount_paise=round(order.total_amount * 100), currency=order.currency)
    ).encode("utf-8")
    signature = _sign(raw_body)

    def deliver(barrier):
        with app_module.app.test_client() as test_client:
            barrier.wait()
            resp = test_client.post(WEBHOOK_URL, data=raw_body, headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})
            return resp.status_code, resp.get_json()

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, barrier) for _ in range(2)]
        results = [f.result() for f in futures]

    assert all(status == 200 for status, _ in results)
    replay_flags = sorted(bool(body.get("replay")) for _, body in results)
    assert replay_flags == [False, True]

    db_session.expire_all()
    assert app_module.PaymentOrder.query.get(order.id).status == "success"
    events = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_concurrent_wh").all()
    assert len(events) == 1
    assert events[0].attempt_count == 2


def test_browser_verify_then_webhook_activates_exactly_once(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    email_calls = _mock_email(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClientOK())
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_browser_then_wh")
    _reserved_reservation(app_module, db_session, normal_user, order)
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    browser_resp = client.post("/verify-payment", data=_verify_form("order_browser_then_wh", payment_id="pay_shared_1"))
    assert browser_resp.get_json()["success"] is True
    first_end = app_module.PaymentOrder.query.get(order.id).subscription_end

    payload = _payment_captured_payload("order_browser_then_wh", payment_id="pay_shared_1", amount_paise=round(order.total_amount * 100), currency=order.currency)
    webhook_resp = _post_event(client, payload)
    assert webhook_resp.status_code == 200

    refreshed = app_module.PaymentOrder.query.get(order.id)
    assert refreshed.subscription_end == first_end
    assert refreshed.status == "success"
    assert len(email_calls) == 1, "webhook must not resend the activation email for an already-activated order"


def test_webhook_then_browser_verify_activates_exactly_once(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    email_calls = _mock_email(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClientOK())
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_wh_then_browser")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_wh_then_browser", payment_id="pay_shared_2", amount_paise=round(order.total_amount * 100), currency=order.currency)
    webhook_resp = _post_event(client, payload)
    assert webhook_resp.status_code == 200
    first_end = app_module.PaymentOrder.query.get(order.id).subscription_end

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    browser_resp = client.post("/verify-payment", data=_verify_form("order_wh_then_browser", payment_id="pay_shared_2"))
    assert browser_resp.get_json()["success"] is True

    refreshed = app_module.PaymentOrder.query.get(order.id)
    assert refreshed.subscription_end == first_end
    assert len(email_calls) == 1, "browser verify must not resend the activation email for an already-activated order"


def test_browser_and_webhook_concurrent_activates_exactly_once(app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClientOK())
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_true_race")
    _reserved_reservation(app_module, db_session, normal_user, order)
    order_id = order.id
    user_id = normal_user.id
    payload = _payment_captured_payload("order_true_race", payment_id="pay_race", amount_paise=round(order.total_amount * 100), currency=order.currency)
    raw_body = json.dumps(payload).encode("utf-8")
    signature = _sign(raw_body)

    def via_webhook(barrier):
        with app_module.app.test_client() as test_client:
            barrier.wait()
            resp = test_client.post(WEBHOOK_URL, data=raw_body, headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})
            return "webhook", resp.status_code, resp.get_json()

    def via_browser(barrier):
        with app_module.app.test_client() as test_client:
            with test_client.session_transaction() as sess:
                sess["user_id"] = user_id
            barrier.wait()
            resp = test_client.post("/verify-payment", data=_verify_form("order_true_race", payment_id="pay_race"))
            return "browser", resp.status_code, resp.get_json()

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(via_webhook, barrier), executor.submit(via_browser, barrier)]
        results = [f.result() for f in futures]

    for _, status, _body in results:
        assert status == 200

    db_session.expire_all()
    refreshed_order = app_module.PaymentOrder.query.get(order_id)
    assert refreshed_order.status == "success"
    refreshed_user = app_module.User.query.get(user_id)
    assert refreshed_user.subscription_status == "active"
    reservation = app_module.PaymentReservation.query.filter_by(payment_order_id=order_id).first()
    assert reservation.status == "activated"


def test_no_duplicate_capacity_or_quota_across_activation_paths(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_no_dup_capacity")
    _reserved_reservation(app_module, db_session, normal_user, order)
    config_before = app_module.CapacityConfig.query.get(1).consumed_count

    payload = _payment_captured_payload("order_no_dup_capacity", amount_paise=round(order.total_amount * 100), currency=order.currency)
    first = _post_event(client, payload)
    assert first.status_code == 200

    user = app_module.User.query.get(normal_user.id)
    user.projects_used = 1
    db_session.commit()

    second = _post_event(client, payload)  # exact replay
    assert second.status_code == 200

    db_session.expire_all()
    assert app_module.CapacityConfig.query.get(1).consumed_count == config_before, "activation must never change capacity consumed_count"
    assert app_module.User.query.get(normal_user.id).projects_used == 1, "replay must not reset usage counters"


# ---------------------------------------------------------------------------
# 4. Reservation lifecycle via webhook
# ---------------------------------------------------------------------------
def test_reserved_reservation_activates_normally_via_webhook(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_reserved_ok")
    reservation = _reserved_reservation(app_module, db_session, normal_user, order, status="reserved")

    payload = _payment_captured_payload("order_reserved_ok", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "success"
    assert app_module.PaymentReservation.query.get(reservation.id).status == "activated"


def test_released_reservation_cannot_activate_via_webhook(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_released")
    _reserved_reservation(app_module, db_session, normal_user, order, status="released")

    payload = _payment_captured_payload("order_released", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_released").first()
    assert event.failure_code == "RESERVATION_EXPIRED"


def test_expired_reservation_cannot_activate_via_webhook(client, app_module, db_session, normal_user, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_expired_wh")
    _reserved_reservation(app_module, db_session, normal_user, order, status="reserved", expires_delta=timedelta(minutes=-5))

    payload = _payment_captured_payload("order_expired_wh", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_expired_wh").first()
    assert event.failure_code == "RESERVATION_EXPIRED"


def test_reservation_belonging_to_different_order_owner_cannot_activate(client, app_module, db_session, normal_user, plan, monkeypatch):
    _with_webhook_secret(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_wrong_owner")
    other_user = _second_user(app_module, db_session, plan)
    # Structurally defensive scenario: a reservation attached to this order
    # but owned by a different user than the order itself.
    reservation = app_module.PaymentReservation(
        user_id=other_user.id,
        payment_order_id=order.id,
        status="reserved",
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )
    db_session.add(reservation)
    db_session.commit()

    payload = _payment_captured_payload("order_wrong_owner", amount_paise=round(order.total_amount * 100), currency=order.currency)
    response = _post_event(client, payload)

    assert response.status_code == 200
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_wrong_owner").first()
    assert event.failure_code == "RESERVATION_MISMATCH"


# ---------------------------------------------------------------------------
# 5. Database-level idempotency enforcement
# ---------------------------------------------------------------------------
def test_idempotency_key_uniqueness_enforced_at_db_level(app_module, db_session):
    first = app_module.RazorpayWebhookEvent(
        idempotency_key="dup-key-test",
        event_type="payment.captured",
        payload_hash="a" * 64,
        processing_status="received",
    )
    db_session.add(first)
    db_session.commit()

    second = app_module.RazorpayWebhookEvent(
        idempotency_key="dup-key-test",
        event_type="payment.captured",
        payload_hash="b" * 64,
        processing_status="received",
    )
    db_session.add(second)
    with pytest.raises(app_module.IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 6. Logging safety
# ---------------------------------------------------------------------------
def test_webhook_logs_no_secret_or_payload_but_logs_safe_metadata(client, app_module, db_session, normal_user, monkeypatch, caplog):
    _with_webhook_secret(app_module, monkeypatch)
    _mock_email(app_module, monkeypatch)
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_log_check")
    _reserved_reservation(app_module, db_session, normal_user, order)

    payload = _payment_captured_payload("order_log_check", payment_id="pay_log_check", amount_paise=round(order.total_amount * 100), currency=order.currency)
    raw_body = json.dumps(payload).encode("utf-8")
    signature = _sign(raw_body)

    import logging
    caplog.set_level(logging.INFO, logger=app_module.app.logger.name)
    response = client.post(WEBHOOK_URL, data=raw_body, headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})
    assert response.status_code == 200

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert WEBHOOK_SECRET not in log_text
    assert signature not in log_text
    assert normal_user.email not in log_text
    assert '"payment"' not in log_text  # no raw payload/entity dump

    assert "razorpay_webhook_processed" in log_text
    assert "payment.captured" in log_text
    assert f"order_id={order.id}" in log_text
