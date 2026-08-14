import hashlib
import hmac
import json

import razorpay
from werkzeug.security import generate_password_hash


class FakeRazorpayOrder:
    def create(self, data):
        return {"id": "order_addon_fake", **data}


class FakeRazorpayUtility:
    def __init__(self, fail=False):
        self.fail = fail

    def verify_payment_signature(self, params):
        if self.fail:
            raise razorpay.errors.SignatureVerificationError("bad signature")
        return True


class FakeRazorpayClient:
    def __init__(self, fail_signature=False):
        self.order = FakeRazorpayOrder()
        self.utility = FakeRazorpayUtility(fail_signature)


def _addon(app_module, db_session, code="EXTRA_100", addon_type="EXTRA_SCANS", available=True):
    item = app_module.AddonCatalog(
        code=code,
        name=code.replace("_", " ").title(),
        addon_type=addon_type,
        unit_amount=99.0,
        currency="INR",
        scan_delta=100 if addon_type == "EXTRA_SCANS" else None,
        validity_days_delta=30 if addon_type == "VALIDITY_EXTENSION" else None,
        project_delta=1 if addon_type == "PROJECT_CAPACITY" else None,
        is_active=True,
        is_commercially_available=available,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _login(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _second_user(app_module, db_session, plan):
    user = app_module.User(
        email="addon-second@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _captured_payload(order_id, payment_id="pay_addon_1", amount_paise=9900, currency="INR"):
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount_paise,
                    "currency": currency,
                    "status": "captured",
                    "order_id": order_id,
                }
            }
        },
        "created_at": 1700000000,
    }


def _sign(raw_body, secret):
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload, secret="whsec_addon_test"):
    raw = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/razorpay",
        data=raw,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(raw, secret)},
    )


def test_addon_catalog_returns_only_commercially_available_items(client, app_module, db_session, normal_user):
    extra = _addon(app_module, db_session, "EXTRA_100")
    _addon(app_module, db_session, "PROJECT_PLUS", addon_type="PROJECT_CAPACITY", available=False)
    _login(client, normal_user)

    response = client.get("/api/addons/catalog")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["success"] is True
    assert [item["id"] for item in payload["addons"]] == [extra.id]


def test_create_addon_order_uses_server_price(client, app_module, db_session, normal_user, monkeypatch):
    item = _addon(app_module, db_session)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    _login(client, normal_user)

    response = client.post("/api/addons/orders", json={"catalog_id": item.id, "quantity": 2, "amount": 1})
    payload = response.get_json()

    assert response.status_code == 201
    assert payload["success"] is True
    assert payload["amount"] == 19800
    purchase = app_module.AddonPurchase.query.filter_by(razorpay_order_id="order_addon_fake").first()
    assert purchase is not None
    assert purchase.total_amount == 198.0


def test_addon_verification_fulfills_once_and_records_ledger(client, app_module, db_session, normal_user, monkeypatch):
    item = _addon(app_module, db_session)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_TEST_1",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_addon_verify",
    )
    original_limit = normal_user.subscribed_scan_limit
    db_session.add(purchase)
    db_session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, normal_user)

    form = {
        "razorpay_payment_id": "pay_addon_verify",
        "razorpay_order_id": "order_addon_verify",
        "razorpay_signature": "sig",
    }
    first = client.post(f"/api/addons/purchases/{purchase.id}/verify", data=form)
    second = client.post(f"/api/addons/purchases/{purchase.id}/verify", data=form)

    assert first.status_code == 200
    assert second.status_code == 200
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscribed_scan_limit == original_limit + 100
    assert app_module.EntitlementTransaction.query.filter_by(
        user_id=normal_user.id,
        source_type="addon_purchase",
        source_id=purchase.id,
        entitlement_type="EXTRA_SCANS",
    ).count() == 1


def test_addon_cross_user_verification_is_rejected(client, app_module, db_session, normal_user, plan, monkeypatch):
    item = _addon(app_module, db_session)
    other = _second_user(app_module, db_session, plan)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_CROSS",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_cross",
    )
    db_session.add(purchase)
    db_session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, other)

    response = client.post(
        f"/api/addons/purchases/{purchase.id}/verify",
        data={"razorpay_payment_id": "pay_cross", "razorpay_order_id": "order_cross", "razorpay_signature": "sig"},
    )

    assert response.status_code == 404
    assert app_module.EntitlementTransaction.query.count() == 0


def test_project_capacity_addon_is_disabled_for_self_service(client, app_module, db_session, normal_user, monkeypatch):
    item = _addon(app_module, db_session, "PROJECT_PLUS", addon_type="PROJECT_CAPACITY", available=False)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, normal_user)

    response = client.post("/api/addons/orders", json={"catalog_id": item.id})

    assert response.status_code == 400
    assert response.get_json()["code"] == "ADDON_UNAVAILABLE"
    assert app_module.AddonPurchase.query.count() == 0


def test_addon_webhook_fulfills_and_replay_does_not_double_credit(client, app_module, db_session, normal_user, monkeypatch):
    secret = "whsec_addon_test"
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", secret)
    item = _addon(app_module, db_session)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_WEBHOOK",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_addon_webhook",
    )
    original_limit = normal_user.subscribed_scan_limit
    db_session.add(purchase)
    db_session.commit()

    payload = _captured_payload("order_addon_webhook", amount_paise=9900)
    first = _post_webhook(client, payload, secret)
    second = _post_webhook(client, payload, secret)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["replay"] is True
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscribed_scan_limit == original_limit + 100
    assert app_module.EntitlementTransaction.query.filter_by(source_id=purchase.id).count() == 1


def test_addon_browser_verify_then_webhook_replay_does_not_double_credit(
    client, app_module, db_session, normal_user, monkeypatch
):
    secret = "whsec_addon_test"
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    item = _addon(app_module, db_session)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_RACE",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_addon_race",
    )
    original_limit = normal_user.subscribed_scan_limit
    db_session.add(purchase)
    db_session.commit()
    _login(client, normal_user)

    verified = client.post(
        f"/api/addons/purchases/{purchase.id}/verify",
        data={
            "razorpay_payment_id": "pay_addon_race",
            "razorpay_order_id": "order_addon_race",
            "razorpay_signature": "sig",
        },
    )
    replay = _post_webhook(client, _captured_payload("order_addon_race", "pay_addon_race"), secret)

    assert verified.status_code == 200
    assert replay.status_code == 200
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscribed_scan_limit == original_limit + 100
    assert app_module.EntitlementTransaction.query.filter_by(source_id=purchase.id).count() == 1


def test_addon_webhook_amount_mismatch_grants_nothing(client, app_module, db_session, normal_user, monkeypatch):
    secret = "whsec_addon_test"
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", secret)
    item = _addon(app_module, db_session)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_BAD_AMOUNT",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_addon_bad_amount",
    )
    original_limit = normal_user.subscribed_scan_limit
    db_session.add(purchase)
    db_session.commit()

    response = _post_webhook(client, _captured_payload("order_addon_bad_amount", amount_paise=1), secret)

    assert response.status_code == 200
    event = app_module.RazorpayWebhookEvent.query.filter_by(razorpay_order_id="order_addon_bad_amount").one()
    assert event.processing_status == "failed"
    assert event.failure_code == "amount_mismatch"
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscribed_scan_limit == original_limit
    assert app_module.EntitlementTransaction.query.count() == 0


def test_addon_signature_failure_grants_nothing(client, app_module, db_session, normal_user, monkeypatch):
    item = _addon(app_module, db_session)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_BAD_SIG",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id="order_bad_sig",
    )
    db_session.add(purchase)
    db_session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(fail_signature=True))
    _login(client, normal_user)

    response = client.post(
        f"/api/addons/purchases/{purchase.id}/verify",
        data={"razorpay_payment_id": "pay_bad_sig", "razorpay_order_id": "order_bad_sig", "razorpay_signature": "sig"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "SIGNATURE_INVALID"
    assert app_module.EntitlementTransaction.query.count() == 0
