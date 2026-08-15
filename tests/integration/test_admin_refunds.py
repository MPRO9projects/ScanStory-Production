import hashlib
import hmac
import json
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


class FakeRefundApi:
    def __init__(self, status="processed", fail=False):
        self.status = status
        self.fail = fail
        self.calls = []

    def refund(self, payment_id, data):
        self.calls.append((payment_id, data))
        if self.fail:
            raise RuntimeError("provider down secret-noise")
        return {
            "id": f"rfnd_fake_{len(self.calls)}",
            "payment_id": payment_id,
            "amount": data["amount"],
            "currency": "INR",
            "status": self.status,
        }


class FakeRazorpayClient:
    def __init__(self, status="processed", fail=False):
        self.payment = FakeRefundApi(status=status, fail=fail)


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


def _user(app_module, db_session, email, *, project_limit=3, scan_limit=100, used=0, active=True):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active" if active else "expired",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30) if active else datetime.utcnow() - timedelta(days=1),
        subscribed_project_limit=project_limit,
        subscribed_scan_limit=scan_limit,
        projects_used=used,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _project(app_module, db_session, owner):
    project = app_module.Project(
        name="Refund Project",
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=1,
        scanner_url="/scanner/refund",
        qr_code_filename="project_refund_main.png",
        qr_code_path="/qr/project_refund_main.png",
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _catalog(app_module, db_session, code, addon_type, **deltas):
    item = app_module.AddonCatalog(
        code=code,
        name=code,
        addon_type=addon_type,
        unit_amount=99.0,
        currency="INR",
        scan_delta=deltas.get("scan_delta"),
        validity_days_delta=deltas.get("validity_days_delta"),
        project_delta=deltas.get("project_delta"),
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _purchase(app_module, db_session, user, item, *, project=None, suffix="1", payment_id="pay_refund_1"):
    purchase = app_module.AddonPurchase(
        order_id=f"ADDON_REFUND_{item.code}_{suffix}",
        user_id=user.id,
        catalog_id=item.id,
        project_id=project.id if project else None,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id=f"order_refund_{item.code}_{suffix}",
        razorpay_payment_id=payment_id,
    )
    db_session.add(purchase)
    db_session.commit()
    return purchase


def _fulfilled_purchase(app_module, db_session, user, item, *, project=None, suffix="1", payment_id="pay_refund_1"):
    purchase = _purchase(app_module, db_session, user, item, project=project, suffix=suffix, payment_id=payment_id)
    result = app_module.fulfill_addon_purchase(purchase)
    assert result["success"] is True
    db_session.refresh(purchase)
    return purchase


def _payment_order(app_module, db_session, user, plan, *, status="success", payment_id="pay_sub_refund"):
    order = app_module.PaymentOrder(
        order_id=f"ORD_REFUND_{user.id}",
        razorpay_order_id=f"order_sub_refund_{user.id}",
        razorpay_payment_id=payment_id,
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        offer_amount=plan.offer_price,
        total_amount=plan.effective_price,
        currency=plan.currency,
        status=status,
        purchased_project_limit=plan.total_project_limit,
        purchased_scan_limit=plan.total_scan_limit,
        subscription_start=datetime.utcnow(),
        subscription_end=datetime.utcnow() + timedelta(days=30),
        payment_at=datetime.utcnow(),
    )
    db_session.add(order)
    db_session.commit()
    return order


def _sign(raw_body, secret):
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload, secret="whsec_refund_test"):
    raw = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/razorpay",
        data=raw,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(raw, secret)},
    )


def _refund_payload(refund_id="rfnd_fake_1", payment_id="pay_refund_pending", status="processed", amount=9900):
    return {
        "entity": "event",
        "event": "refund.processed" if status == "processed" else "refund.failed",
        "payload": {
            "refund": {
                "entity": {
                    "id": refund_id,
                    "amount": amount,
                    "currency": "INR",
                    "payment_id": payment_id,
                    "status": status,
                }
            },
            "payment": {"entity": {"id": payment_id, "order_id": "order_unused"}},
        },
        "created_at": 1700000000,
    }


def test_refund_authorization_user_blocked_admin_allowed_and_csrf_required(
    client, app_module, db_session, normal_user, admin
):
    item = _catalog(app_module, db_session, "CAP_AUTH", "PROJECT_CAPACITY", project_delta=1)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_auth")

    _login_user(client, normal_user)
    assert client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "support"}).status_code != 200

    limited = app_module.Admin(
        email="limited-refund@example.com",
        password_hash=generate_password_hash("AdminPass123"),
        role="admin",
        is_active=True,
    )
    db_session.add(limited)
    db_session.commit()
    _login_admin(client, limited)
    assert client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "support"}).status_code != 200

    app_module.app.config["WTF_CSRF_ENABLED"] = True
    _login_admin(client, admin)
    rejected = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", data={"reason": "support"})
    assert rejected.status_code == 400
    app_module.app.config["WTF_CSRF_ENABLED"] = False


def test_full_refund_api_called_and_capacity_reversed_without_deleting_projects(
    client, app_module, db_session, normal_user, admin, monkeypatch
):
    normal_user.subscribed_project_limit = 8
    normal_user.projects_used = 6
    item = _catalog(app_module, db_session, "CAP_REFUND", "PROJECT_CAPACITY", project_delta=5)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_cap_refund")
    project = _project(app_module, db_session, normal_user)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    response = client.post(
        f"/admin/api/addon-purchases/{purchase.id}/refund",
        json={"reason": "Customer support approval", "idempotency_key": "cap-refund-once"},
    )
    replay = client.post(
        f"/admin/api/addon-purchases/{purchase.id}/refund",
        json={"reason": "Customer support approval", "idempotency_key": "cap-refund-once"},
    )

    assert response.status_code == 200
    assert replay.status_code == 200
    assert replay.get_json()["replay"] is True
    assert app_module.razorpay_client.payment.calls == [
        ("pay_cap_refund", {"amount": 9900, "notes": {"refund_id": "1", "source": "addon_purchase", "admin_id": str(admin.id)}})
    ]
    db_session.refresh(normal_user)
    db_session.refresh(project)
    assert normal_user.subscribed_project_limit == 8
    assert normal_user.projects_used == 6
    assert app_module.project_capacity_summary(normal_user)["over_capacity"] is False
    assert app_module.Project.query.get(project.id) is not None
    assert app_module.EntitlementTransaction.query.filter_by(
        source_type="addon_purchase", source_id=purchase.id, entitlement_type="PROJECT_CAPACITY"
    ).count() == 1
    reversal = app_module.EntitlementTransaction.query.filter_by(
        source_type="refund", entitlement_type="PROJECT_CAPACITY"
    ).one()
    assert reversal.delta_value == -5


def test_capacity_refund_can_leave_account_over_capacity_and_blocks_new_reserve(
    client, app_module, db_session, admin, monkeypatch
):
    user = _user(app_module, db_session, "over-capacity@example.com", project_limit=3, used=6)
    item = _catalog(app_module, db_session, "CAP_OVER", "PROJECT_CAPACITY", project_delta=5)
    purchase = _fulfilled_purchase(app_module, db_session, user, item, payment_id="pay_cap_over")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    assert client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "refund"}).status_code == 200

    db_session.refresh(user)
    assert user.subscribed_project_limit == 3
    assert user.projects_used == 6
    assert app_module.project_capacity_summary(user)["over_capacity"] is True
    assert app_module._reserve_project_quota_atomic(user) is False
    user.projects_used = 2
    user.subscription_status = "active"
    db_session.commit()
    assert app_module._reserve_project_quota_atomic(user) is True


def test_provider_failure_records_no_reversal(client, app_module, db_session, normal_user, admin, monkeypatch):
    item = _catalog(app_module, db_session, "SCANS_FAIL", "EXTRA_SCANS", scan_delta=100)
    original_limit = normal_user.subscribed_scan_limit
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_fail")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(fail=True))
    _login_admin(client, admin)

    response = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "support"})

    assert response.status_code == 409
    refund = app_module.PaymentRefund.query.one()
    assert refund.status == "REFUND_FAILED"
    assert refund.reconciliation_status == "PENDING"
    assert app_module.EntitlementTransaction.query.filter_by(source_type="refund").count() == 0
    db_session.refresh(normal_user)
    assert normal_user.subscribed_scan_limit == original_limit + 100


def test_project_renewal_refund_revokes_exact_unstarted_latest_coverage(
    client, app_module, db_session, admin, monkeypatch
):
    owner = _user(app_module, db_session, "renew-refund@example.com")
    owner.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    project = _project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_REFUND", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = _fulfilled_purchase(app_module, db_session, owner, item, project=project, payment_id="pay_renew_refund")
    coverage = app_module.ProjectServiceCoverage.query.filter_by(
        project_id=project.id, source_type="STANDALONE_PROJECT_RENEWAL"
    ).one()
    assert coverage.coverage_start > datetime.utcnow()
    qr = project.qr_code_filename
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    response = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "future renewal refund"})

    assert response.status_code == 200
    db_session.refresh(coverage)
    db_session.refresh(project)
    assert coverage.status == "REVOKED"
    assert coverage.revoked_by_refund_id == response.get_json()["refund"]["id"]
    assert project.qr_code_filename == qr
    assert app_module.Project.query.get(project.id) is not None


def test_renewal_refund_blocks_older_chained_and_consumed_periods(client, app_module, db_session, admin):
    owner = _user(app_module, db_session, "renew-chain-refund@example.com", project_limit=5)
    project = _project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_CHAIN_REFUND", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    first = _fulfilled_purchase(app_module, db_session, owner, item, project=project, suffix="a", payment_id="pay_renew_a")
    second = _fulfilled_purchase(app_module, db_session, owner, item, project=project, suffix="b", payment_id="pay_renew_b")

    older = client.get(f"/admin/api/addon-purchases/{first.id}/refund-eligibility")
    assert older.status_code != 200  # not logged in
    _login_admin(client, admin)
    older = client.get(f"/admin/api/addon-purchases/{first.id}/refund-eligibility")
    assert older.get_json()["eligibility"]["reason_code"] == "SUPERSEDED_BY_LATER_RENEWAL"

    active_user = _user(app_module, db_session, "renew-active-refund@example.com", active=False)
    active_project = _project(app_module, db_session, active_user)
    active_item = _catalog(app_module, db_session, "RENEW_ACTIVE_REFUND", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    active_purchase = _fulfilled_purchase(
        app_module, db_session, active_user, active_item, project=active_project, payment_id="pay_renew_active"
    )
    active = client.get(f"/admin/api/addon-purchases/{active_purchase.id}/refund-eligibility")
    assert active.get_json()["eligibility"]["reason_code"] == "INELIGIBLE_CONSUMED_SERVICE"


def test_extra_scans_reversal_preserves_history_and_can_be_over_limit(client, app_module, db_session, admin, monkeypatch):
    user = _user(app_module, db_session, "scan-refund@example.com", scan_limit=100)
    project = _project(app_module, db_session, user)
    item = _catalog(app_module, db_session, "SCANS_REFUND", "EXTRA_SCANS", scan_delta=100)
    purchase = _fulfilled_purchase(app_module, db_session, user, item, payment_id="pay_scans_refund")
    user.scans_used = 150
    db_session.add(app_module.ScanLog(project_id=project.id, user_id=user.id, scan_session_id="hist", is_successful=True, counted=True))
    db_session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    assert client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "support"}).status_code == 200

    db_session.refresh(user)
    assert user.subscribed_scan_limit == 100
    assert user.scans_used == 150
    assert app_module.ScanLog.query.filter_by(user_id=user.id).count() == 1
    assert app_module.EntitlementTransaction.query.filter_by(source_type="refund", entitlement_type="EXTRA_SCANS").one().delta_value == -100


def test_validity_extension_and_subscription_refunds_are_manual_reconciliation(
    client, app_module, db_session, normal_user, admin, plan, monkeypatch
):
    validity = _catalog(app_module, db_session, "VALIDITY_REFUND", "VALIDITY_EXTENSION", validity_days_delta=30)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, validity, payment_id="pay_validity_refund")
    original_expiry = normal_user.subscription_expires_at
    order = _payment_order(app_module, db_session, normal_user, plan, payment_id="pay_sub_manual")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    vresp = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "manual"})
    sresp = client.post(f"/admin/api/payments/{order.id}/refund", json={"reason": "manual"})

    assert vresp.status_code == 200
    assert vresp.get_json()["refund"]["reconciliation_status"] == "MANUAL_REVIEW_REQUIRED"
    assert sresp.status_code == 200
    assert sresp.get_json()["refund"]["reconciliation_status"] == "MANUAL_REVIEW_REQUIRED"
    db_session.refresh(normal_user)
    assert normal_user.subscription_expires_at == original_expiry
    db_session.refresh(order)
    assert order.status == "refunded"


def test_webhook_refund_processed_reconciles_once(client, app_module, db_session, normal_user, admin, monkeypatch):
    item = _catalog(app_module, db_session, "SCANS_WEBHOOK", "EXTRA_SCANS", scan_delta=100)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_refund_pending")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="created"))
    _login_admin(client, admin)
    start = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "pending"})
    assert start.status_code == 200
    refund = app_module.PaymentRefund.query.one()
    assert refund.status == "REFUND_PROCESSING"
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", "whsec_refund_test")

    payload = _refund_payload(payment_id="pay_refund_pending", status="processed")
    first = _post_webhook(client, payload)
    second = _post_webhook(client, payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["replay"] is True
    db_session.refresh(refund)
    assert refund.status == "REFUNDED"
    assert refund.reconciliation_status == "APPLIED"
    assert app_module.EntitlementTransaction.query.filter_by(source_type="refund").count() == 1


def test_webhook_signature_required_and_failure_recorded(client, app_module, db_session, normal_user, admin, monkeypatch):
    item = _catalog(app_module, db_session, "SCANS_WEBHOOK_FAIL", "EXTRA_SCANS", scan_delta=100)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_refund_fail")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="created"))
    _login_admin(client, admin)
    assert client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "pending"}).status_code == 200
    monkeypatch.setattr(app_module, "RAZORPAY_WEBHOOK_SECRET", "whsec_refund_test")

    unsigned = client.post("/webhooks/razorpay", json=_refund_payload(payment_id="pay_refund_fail", status="failed"))
    signed = _post_webhook(client, _refund_payload(payment_id="pay_refund_fail", status="failed"))

    assert unsigned.status_code == 400
    assert signed.status_code == 200
    refund = app_module.PaymentRefund.query.one()
    assert refund.status == "REFUND_FAILED"
    assert refund.reconciliation_status == "PENDING"


def test_refund_detail_contract_and_audit_records(client, app_module, db_session, normal_user, admin, monkeypatch):
    item = _catalog(app_module, db_session, "CAP_DETAIL", "PROJECT_CAPACITY", project_delta=1)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item, payment_id="pay_detail")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    _login_admin(client, admin)

    response = client.post(f"/admin/api/addon-purchases/{purchase.id}/refund", json={"reason": "detail"})
    refund_id = response.get_json()["refund"]["id"]
    detail = client.get(f"/admin/api/refunds/{refund_id}")

    assert detail.status_code == 200
    refund = detail.get_json()["refund"]
    assert refund["provider_refund_id"] == "rfnd_fake_1"
    assert "secret" not in json.dumps(refund).lower()
    activity_types = {row.activity_type for row in app_module.AdminActivity.query.all()}
    assert {"refund_requested", "refund_provider_attempted", "refund_confirmed", "refund_reconciliation"} <= activity_types
