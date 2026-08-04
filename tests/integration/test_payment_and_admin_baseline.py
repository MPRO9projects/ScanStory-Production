from datetime import datetime, timedelta


class FakeRazorpayOrder:
    def create(self, data):
        return {"id": "order_fake_123", **data}


class FakeRazorpayUtility:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def verify_payment_signature(self, params):
        if self.should_fail:
            import razorpay

            raise razorpay.errors.SignatureVerificationError("bad signature")
        return True


class FakeRazorpayClient:
    def __init__(self, should_fail=False):
        self.order = FakeRazorpayOrder()
        self.utility = FakeRazorpayUtility(should_fail)


def test_plan_listing(client, login_user):
    response = client.get("/subscribe")
    assert response.status_code == 200
    assert b"Basic" in response.data or b"Free Trial" in response.data


def test_create_razorpay_order_uses_mock(client, app_module, normal_user, monkeypatch):
    paid_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["order_id"] == "order_fake_123"
    assert app_module.PaymentOrder.query.filter_by(razorpay_order_id="order_fake_123").first() is not None


def test_verify_payment_success_activates_subscription(client, app_module, normal_user, monkeypatch):
    paid_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    order = app_module.PaymentOrder(
        order_id="ORD_TEST",
        razorpay_order_id="order_fake_456",
        user_id=normal_user.id,
        plan_id=paid_plan.id,
        amount=paid_plan.plan_amount,
        total_amount=paid_plan.effective_price,
        currency=paid_plan.currency,
        status="pending",
    )
    app_module.db.session.add(order)
    app_module.db.session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    monkeypatch.setattr(app_module, "send_payment_success_email", lambda user, plan, order: True)
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    response = client.post(
        "/verify-payment",
        data={
            "razorpay_payment_id": "pay_123",
            "razorpay_order_id": "order_fake_456",
            "razorpay_signature": "sig",
        },
    )
    assert response.get_json()["success"] is True
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscription_status == "active"
    assert order.status == "success"


def test_admin_login_and_dashboard(client, admin, admin_password):
    response = client.post("/admin/login", data={"email": admin.email, "password": admin_password})
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess["admin_id"] == admin.id
    dashboard = client.get("/admin/dashboard")
    assert dashboard.status_code == 200


def test_admin_login_rejects_removed_default_password(client, admin):
    """The old "Admin@123" default was removed from the application; the
    real admin (created here with an explicit test password, see
    admin_password/isolated_app) must not accept it."""
    response = client.post("/admin/login", data={"email": admin.email, "password": "Admin@123"})
    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert "admin_id" not in sess


def test_normal_user_denied_admin_dashboard(client, login_user):
    response = client.get("/admin/dashboard")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]
