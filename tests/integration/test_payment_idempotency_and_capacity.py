"""V1 Phase 2 tests: payment verification idempotency + paid-account capacity.

Extends the existing Razorpay mocking approach from
tests/integration/test_payment_and_admin_baseline.py (FakeRazorpayOrder /
FakeRazorpayUtility / FakeRazorpayClient monkeypatched onto app_module) -
no network calls, no real credentials, anywhere in these tests.
"""
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Shared fakes (same shape as test_payment_and_admin_baseline.py)
# ---------------------------------------------------------------------------
class FakeRazorpayOrder:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def create(self, data):
        if self.should_fail:
            raise RuntimeError("simulated Razorpay order creation failure")
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
    def __init__(self, should_fail_order=False, should_fail_signature=False):
        self.order = FakeRazorpayOrder(should_fail_order)
        self.utility = FakeRazorpayUtility(should_fail_signature)


def _run_competing_calls(worker, count=2):
    """Same helper as tests/integration/test_quota_characterization.py's
    _run_competing_calls - two workers released simultaneously via a Barrier,
    run concurrently under ThreadPoolExecutor."""
    barrier = Barrier(count)
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(worker, barrier) for _ in range(count)]
        return [future.result() for future in futures]


def _second_user(app_module, db_session, plan, email="second@example.com"):
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


def _login(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _paid_plan(app_module):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()


def _make_pending_order(app_module, db_session, user, plan, razorpay_order_id, order_id="ORD_TEST"):
    order = app_module.PaymentOrder(
        order_id=order_id,
        razorpay_order_id=razorpay_order_id,
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.effective_price,
        currency=plan.currency,
        status="pending",
    )
    db_session.add(order)
    db_session.commit()
    return order


def _attach_reservation(app_module, db_session, user, order, status="reserved", expires_delta=timedelta(minutes=30)):
    reservation = app_module.PaymentReservation(
        user_id=user.id,
        payment_order_id=order.id,
        status=status,
        expires_at=datetime.utcnow() + expires_delta,
    )
    db_session.add(reservation)
    config = app_module._get_or_create_capacity_config()
    config.consumed_count = 1 if status in ("reserved", "activated") else 0
    db_session.commit()
    return reservation


def _verify(client, order_id, payment_id="pay_1", signature="sig", **extra):
    data = {
        "razorpay_payment_id": payment_id,
        "razorpay_order_id": order_id,
        "razorpay_signature": signature,
    }
    data.update(extra)
    return client.post("/verify-payment", data=data)


# ---------------------------------------------------------------------------
# 1. Order creation success (also reserves a capacity slot)
# ---------------------------------------------------------------------------
def test_order_creation_success_reserves_capacity_slot(client, app_module, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    _login(client, normal_user)

    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})
    payload = response.get_json()

    assert payload["success"] is True
    order = app_module.PaymentOrder.query.filter_by(razorpay_order_id="order_fake_123").first()
    assert order is not None
    reservation = app_module.PaymentReservation.query.filter_by(payment_order_id=order.id).first()
    assert reservation is not None
    assert reservation.status == "reserved"
    assert reservation.user_id == normal_user.id
    config = app_module.CapacityConfig.query.get(1)
    assert config.consumed_count == 1


# ---------------------------------------------------------------------------
# 2. Signature verification failure
# ---------------------------------------------------------------------------
def test_verify_payment_signature_failure_is_rejected(client, app_module, db_session, normal_user, plan, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_sig_fail")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(should_fail_signature=True))
    _login(client, normal_user)

    response = _verify(client, "order_sig_fail")

    assert response.get_json()["success"] is False
    assert "Invalid payment signature" in response.get_json()["error"]
    refreshed = app_module.PaymentOrder.query.get(order.id)
    assert refreshed.status == "pending"


# ---------------------------------------------------------------------------
# 3. Callback replay is idempotent (no double reset / no double activation)
# ---------------------------------------------------------------------------
def test_callback_replay_is_idempotent_no_op(client, app_module, db_session, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_replay")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    email_calls = []
    monkeypatch.setattr(app_module, "send_payment_success_email", lambda user, plan, order: email_calls.append(1))
    _login(client, normal_user)

    first = _verify(client, "order_replay", payment_id="pay_replay")
    assert first.get_json()["success"] is True
    first_end = app_module.PaymentOrder.query.get(order.id).subscription_end

    # Simulate real usage happening after activation.
    user = app_module.User.query.get(normal_user.id)
    user.projects_used = 1
    db_session.commit()

    second = _verify(client, "order_replay", payment_id="pay_replay")
    assert second.get_json()["success"] is True

    refreshed_user = app_module.User.query.get(normal_user.id)
    refreshed_order = app_module.PaymentOrder.query.get(order.id)
    assert refreshed_user.projects_used == 1, "replay must not reset usage counters again"
    assert refreshed_order.subscription_end == first_end, "replay must not re-extend subscription_end"
    assert len(email_calls) == 1, "replay must not resend the success email"

    reservation = app_module.PaymentReservation.query.filter_by(payment_order_id=order.id).first()
    if reservation:
        assert reservation.status == "activated"


# ---------------------------------------------------------------------------
# 4. Concurrent final-slot reservation: exactly one succeeds
# ---------------------------------------------------------------------------
def test_concurrent_final_slot_reservation_allows_only_one(app_module, db_session, normal_user, plan):
    config = app_module._get_or_create_capacity_config()
    config.configured_limit = 1
    config.consumed_count = 0
    db_session.commit()
    second = _second_user(app_module, db_session, plan)
    user_ids = [normal_user.id, second.id]

    def reserve(barrier, user_id):
        with app_module.app.app_context():
            user = app_module.User.query.get(user_id)
            barrier.wait()
            reservation = app_module._reserve_capacity_slot_atomic(user)
            app_module.db.session.remove()
            return reservation is not None

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve, barrier, uid) for uid in user_ids]
        results = [f.result() for f in futures]

    assert sorted(results) == [False, True]
    db_session.expire_all()
    final_config = app_module.CapacityConfig.query.get(1)
    assert final_config.consumed_count == 1
    assert app_module.PaymentReservation.query.filter_by(status="reserved").count() == 1


def test_atomic_capacity_reservation_at_limit_minus_one(app_module, db_session, normal_user):
    config = app_module._get_or_create_capacity_config()
    config.configured_limit = 1
    config.consumed_count = 0
    db_session.commit()

    first = app_module._reserve_capacity_slot_atomic(normal_user)
    second = app_module._reserve_capacity_slot_atomic(normal_user)

    assert first is not None
    assert second is None
    assert app_module.CapacityConfig.query.get(1).consumed_count == 1


# ---------------------------------------------------------------------------
# 5. Reservation expiration is not usable for activation
# ---------------------------------------------------------------------------
def test_expired_reservation_blocks_activation(client, app_module, db_session, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_expired")
    reservation = app_module.PaymentReservation(
        user_id=normal_user.id,
        payment_order_id=order.id,
        status="reserved",
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    db_session.add(reservation)
    config = app_module._get_or_create_capacity_config()
    config.consumed_count = 1
    db_session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, normal_user)

    response = _verify(client, "order_expired")
    payload = response.get_json()

    assert payload["success"] is False
    assert payload["code"] == "RESERVATION_EXPIRED"
    refreshed_reservation = app_module.PaymentReservation.query.get(reservation.id)
    assert refreshed_reservation.status == "expired"
    assert app_module.CapacityConfig.query.get(1).consumed_count == 0
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"

    # A later retry of the SAME order must still be rejected, not sneak
    # through and activate for free now that the slot has been freed.
    second_response = _verify(client, "order_expired")
    assert second_response.get_json()["success"] is False
    assert second_response.get_json()["code"] == "RESERVATION_EXPIRED"


def test_reconcile_payment_activations_dry_run_does_not_mutate(app_module, db_session, normal_user):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_reconcile_dry", "ORD_RECON_DRY")
    order.razorpay_payment_id = "pay_reconcile_dry"
    reservation = _attach_reservation(app_module, db_session, normal_user, order)
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations"])

    assert result.exit_code == 0
    assert "Mode: dry-run" in result.output
    assert "Candidates: 1" in result.output
    assert "Activated: 0" in result.output
    assert "Skipped: 1" in result.output
    db_session.expire_all()
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    assert app_module.PaymentReservation.query.get(reservation.id).status == "reserved"
    assert app_module.User.query.get(normal_user.id).subscription_status == "trial"


def test_reconcile_payment_activations_apply_activates_entitlement_once(app_module, db_session, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    normal_user.projects_used = 4
    normal_user.scans_used = 9
    db_session.commit()
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_reconcile_apply", "ORD_RECON_APPLY")
    order.razorpay_payment_id = "pay_reconcile_apply"
    reservation = _attach_reservation(app_module, db_session, normal_user, order)
    db_session.commit()
    monkeypatch.setattr(
        app_module,
        "send_payment_success_email",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CLI must not send email")),
    )

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations", "--apply"])

    assert result.exit_code == 0
    assert "Activated: 1" in result.output
    assert "Failed: 0" in result.output
    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    refreshed_order = app_module.PaymentOrder.query.get(order.id)
    assert refreshed_order.status == "success"
    assert user.subscription_id == paid_plan.id
    assert user.subscription_status == "active"
    assert user.subscribed_project_limit == paid_plan.total_project_limit
    assert user.subscribed_scan_limit == paid_plan.total_scan_limit
    assert user.projects_used == 0
    assert user.scans_used == 0
    assert app_module.PaymentReservation.query.get(reservation.id).status == "activated"

    first_end = refreshed_order.subscription_end
    user.projects_used = 3
    user.scans_used = 2
    db_session.commit()

    second = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations", "--apply"])

    assert second.exit_code == 0
    assert "Candidates: 0" in second.output
    db_session.expire_all()
    assert app_module.PaymentOrder.query.get(order.id).subscription_end == first_end
    user = app_module.User.query.get(normal_user.id)
    assert user.projects_used == 3
    assert user.scans_used == 2


def test_reconcile_payment_activations_ignores_pending_order_without_payment_id(app_module, db_session, normal_user):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_reconcile_no_pay", "ORD_RECON_NO_PAY")
    _attach_reservation(app_module, db_session, normal_user, order)

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations", "--apply"])

    assert result.exit_code == 0
    assert "Candidates: 0" in result.output
    db_session.expire_all()
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    assert app_module.User.query.get(normal_user.id).subscription_status == "trial"


@pytest.mark.parametrize("status", ["released", "expired"])
def test_reconcile_payment_activations_skips_released_or_expired_reservation(app_module, db_session, normal_user, status):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(
        app_module, db_session, normal_user, paid_plan, f"order_reconcile_{status}", f"ORD_RECON_{status.upper()}"
    )
    order.razorpay_payment_id = f"pay_reconcile_{status}"
    reservation = _attach_reservation(app_module, db_session, normal_user, order, status=status)
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations", "--apply"])

    assert result.exit_code == 0
    assert "Activated: 0" in result.output
    assert "Skipped: 1" in result.output
    db_session.expire_all()
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"
    assert app_module.PaymentReservation.query.get(reservation.id).status == status
    assert app_module.User.query.get(normal_user.id).subscription_status == "trial"


def test_reconcile_payment_activations_counts_replay_as_skipped(app_module, db_session, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_reconcile_replay", "ORD_RECON_REPLAY")
    order.razorpay_payment_id = "pay_reconcile_replay"
    _attach_reservation(app_module, db_session, normal_user, order)
    db_session.commit()
    calls = []

    def replay(_order):
        calls.append(_order.id)
        return {"success": True, "order_id": _order.order_id, "plan_name": paid_plan.plan_name, "replay": True}

    monkeypatch.setattr(app_module, "activate_payment", replay)

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-payment-activations", "--apply"])

    assert result.exit_code == 0
    assert calls == [order.id]
    assert "Activated: 0" in result.output
    assert "Skipped: 1" in result.output
    assert "Failed: 0" in result.output
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"


# ---------------------------------------------------------------------------
# 6. Order-creation failure releases the reservation
# ---------------------------------------------------------------------------
def test_order_creation_failure_releases_reservation(client, app_module, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(should_fail_order=True))
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    _login(client, normal_user)

    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})

    assert response.get_json()["success"] is False
    assert app_module.PaymentOrder.query.count() == 0
    reservation = app_module.PaymentReservation.query.filter_by(user_id=normal_user.id).first()
    assert reservation is not None
    assert reservation.status == "released"
    assert app_module.CapacityConfig.query.get(1).consumed_count == 0


# ---------------------------------------------------------------------------
# 7. Verification attempted by the wrong user
# ---------------------------------------------------------------------------
def test_verification_by_wrong_user_is_rejected(client, app_module, db_session, normal_user, plan, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_wrong_user")
    other_user = _second_user(app_module, db_session, plan)
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, other_user)

    response = _verify(client, "order_wrong_user")

    assert response.get_json()["success"] is False
    assert "Invalid payment order" in response.get_json()["error"]
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"


# ---------------------------------------------------------------------------
# 8. Amount/plan mismatch rejection
# ---------------------------------------------------------------------------
def test_plan_mismatch_is_rejected(client, app_module, db_session, normal_user, plan, monkeypatch):
    paid_plan = _paid_plan(app_module)
    other_plan = app_module.SubscriptionPlan.query.filter(
        app_module.SubscriptionPlan.id != paid_plan.id
    ).first()
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_plan_mismatch")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, normal_user)

    response = _verify(client, "order_plan_mismatch", plan_id=other_plan.id)

    assert response.get_json()["success"] is False
    assert "Plan does not match" in response.get_json()["error"]
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"


def test_amount_mismatch_is_rejected(client, app_module, db_session, normal_user, monkeypatch):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_amount_mismatch")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    _login(client, normal_user)

    response = _verify(client, "order_amount_mismatch", amount=str(paid_plan.effective_price + 500))

    assert response.get_json()["success"] is False
    assert "Amount does not match" in response.get_json()["error"]
    assert app_module.PaymentOrder.query.get(order.id).status == "pending"


# ---------------------------------------------------------------------------
# 9. Duplicate Razorpay IDs rejected at the DB level
# ---------------------------------------------------------------------------
def test_duplicate_razorpay_order_id_rejected_by_db(app_module, db_session, normal_user, plan):
    paid_plan = _paid_plan(app_module)
    first = app_module.PaymentOrder(
        order_id="ORD_A", razorpay_order_id="order_dup", user_id=normal_user.id, plan_id=paid_plan.id,
        amount=paid_plan.plan_amount, total_amount=paid_plan.effective_price, currency=paid_plan.currency,
        status="pending",
    )
    db_session.add(first)
    db_session.commit()

    second = app_module.PaymentOrder(
        order_id="ORD_B", razorpay_order_id="order_dup", user_id=normal_user.id, plan_id=paid_plan.id,
        amount=paid_plan.plan_amount, total_amount=paid_plan.effective_price, currency=paid_plan.currency,
        status="pending",
    )
    db_session.add(second)
    with pytest.raises(app_module.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_duplicate_razorpay_payment_id_rejected_by_db(app_module, db_session, normal_user, plan):
    paid_plan = _paid_plan(app_module)
    first = app_module.PaymentOrder(
        order_id="ORD_C", razorpay_order_id="order_c", razorpay_payment_id="pay_dup", user_id=normal_user.id,
        plan_id=paid_plan.id, amount=paid_plan.plan_amount, total_amount=paid_plan.effective_price,
        currency=paid_plan.currency, status="success",
    )
    db_session.add(first)
    db_session.commit()

    second = app_module.PaymentOrder(
        order_id="ORD_D", razorpay_order_id="order_d", razorpay_payment_id="pay_dup", user_id=normal_user.id,
        plan_id=paid_plan.id, amount=paid_plan.plan_amount, total_amount=paid_plan.effective_price,
        currency=paid_plan.currency, status="success",
    )
    db_session.add(second)
    with pytest.raises(app_module.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_multiple_null_razorpay_payment_ids_are_allowed(app_module, db_session, normal_user):
    """Plain unique index, not a partial one - NULL != NULL still applies,
    so two pending orders (no payment id yet) must coexist fine."""
    paid_plan = _paid_plan(app_module)
    for i in range(2):
        order = app_module.PaymentOrder(
            order_id=f"ORD_NULL_{i}", razorpay_order_id=f"order_null_{i}", user_id=normal_user.id,
            plan_id=paid_plan.id, amount=paid_plan.plan_amount, total_amount=paid_plan.effective_price,
            currency=paid_plan.currency, status="pending",
        )
        db_session.add(order)
    db_session.commit()  # must not raise
    assert app_module.PaymentOrder.query.filter_by(razorpay_payment_id=None).count() == 2


# ---------------------------------------------------------------------------
# 10. Activation happens exactly once under a simulated double-call
# ---------------------------------------------------------------------------
def test_concurrent_duplicate_verify_activates_exactly_once(app_module, db_session, normal_user):
    paid_plan = _paid_plan(app_module)
    order = _make_pending_order(app_module, db_session, normal_user, paid_plan, "order_concurrent_activate", order_id="ORD_CONCURRENT")
    order_id = order.id
    user_id = normal_user.id

    def call_activate(barrier):
        with app_module.app.app_context():
            fresh_order = app_module.PaymentOrder.query.get(order_id)
            barrier.wait()
            result = app_module.activate_payment(fresh_order)
            app_module.db.session.remove()
            return result

    results = _run_competing_calls(call_activate)

    assert all(r["success"] for r in results)
    replay_flags = sorted(r["replay"] for r in results)
    assert replay_flags == [False, True], "exactly one caller must do the real activation, the other a replay"

    db_session.expire_all()
    refreshed_user = app_module.User.query.get(user_id)
    assert refreshed_user.subscription_status == "active"
    assert refreshed_user.subscribed_project_limit == paid_plan.total_project_limit


# ---------------------------------------------------------------------------
# 11. Lowering capacity below current active count never deactivates anyone
# ---------------------------------------------------------------------------
def test_lowering_capacity_limit_never_deactivates_existing_active_users(app_module, db_session, normal_user, plan):
    config = app_module._get_or_create_capacity_config()
    config.configured_limit = 25
    config.consumed_count = 0
    db_session.commit()

    second = _second_user(app_module, db_session, plan)
    paid_plan = _paid_plan(app_module)

    for user in (normal_user, second):
        reservation = app_module._reserve_capacity_slot_atomic(user)
        assert reservation is not None
        order = app_module.PaymentOrder(
            order_id=f"ORD_ACTIVE_{user.id}", razorpay_order_id=f"order_active_{user.id}",
            user_id=user.id, plan_id=paid_plan.id, amount=paid_plan.plan_amount,
            total_amount=paid_plan.effective_price, currency=paid_plan.currency, status="pending",
        )
        db_session.add(order)
        db_session.flush()
        reservation.payment_order_id = order.id
        db_session.commit()
        result = app_module.activate_payment(order)
        assert result["success"] is True

    db_session.expire_all()
    assert app_module.User.query.filter_by(subscription_status="active").count() == 2

    # Lower the configured limit below the current active count.
    config = app_module.CapacityConfig.query.get(1)
    config.configured_limit = 1
    db_session.commit()

    # Both existing active users remain untouched.
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).subscription_status == "active"
    assert app_module.User.query.get(second.id).subscription_status == "active"

    # But a brand-new reservation attempt is now correctly rejected (2 slots
    # already consumed by activated reservations >= limit of 1).
    third = _second_user(app_module, db_session, plan, email="third@example.com")
    assert app_module._reserve_capacity_slot_atomic(third) is None


# ---------------------------------------------------------------------------
# 12. Capacity disabled/paused behavior
# ---------------------------------------------------------------------------
def test_capacity_disabled_rejects_new_reservations_even_with_room(app_module, db_session, normal_user):
    """Disabled means "not accepting new paid signups at all" - a deliberate
    choice matching "paused" semantics, even though the counter has plenty of
    room left under configured_limit."""
    config = app_module._get_or_create_capacity_config()
    config.configured_limit = 25
    config.consumed_count = 0
    config.enabled = False
    db_session.commit()

    reservation = app_module._reserve_capacity_slot_atomic(normal_user)

    assert reservation is None
    assert app_module.CapacityConfig.query.get(1).consumed_count == 0


def test_capacity_full_rejects_before_any_razorpay_order_created(client, app_module, normal_user, monkeypatch):
    config = app_module._get_or_create_capacity_config()
    config.configured_limit = 0
    config.consumed_count = 0
    app_module.db.session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    _login(client, normal_user)
    paid_plan = _paid_plan(app_module)

    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["code"] == "CAPACITY_FULL"
    assert payload["success"] is False
    assert app_module.PaymentOrder.query.count() == 0
