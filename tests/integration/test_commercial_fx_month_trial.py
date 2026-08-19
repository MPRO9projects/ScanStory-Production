from datetime import datetime, timedelta


class FakeRazorpayOrder:
    def __init__(self):
        self.requests = []

    def create(self, data):
        self.requests.append(data)
        return {"id": "order_fx_fake", **data}


class FakeRazorpayClient:
    def __init__(self):
        self.order = FakeRazorpayOrder()


def _paid_plan(app_module):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()


def test_dynamic_rate_converts_inr_to_usd_with_deterministic_rounding(app_module, monkeypatch):
    app_module._FX_CACHE["quote"] = None
    monkeypatch.setattr(
        app_module,
        "fetch_inr_per_usd_quote",
        lambda: app_module.FxRateQuote("INR", "USD", 93.0, "test", app_module.dt.utcnow()),
    )

    assert app_module.convert_inr_to_usd(999) == 10.74
    assert app_module.current_inr_per_usd_quote().base_currency == "INR"


def test_fx_cache_reuses_current_rate_within_ttl(app_module, monkeypatch):
    calls = []

    def provider():
        calls.append(1)
        return app_module.FxRateQuote("INR", "USD", 90.0 + len(calls), "test", app_module.dt.utcnow())

    app_module._FX_CACHE["quote"] = None
    monkeypatch.setattr(app_module, "fetch_inr_per_usd_quote", provider)

    first = app_module.current_inr_per_usd_quote()
    second = app_module.current_inr_per_usd_quote()

    assert first.rate == second.rate
    assert len(calls) == 1


def test_fx_provider_failure_uses_safe_stale_or_configured_rate(app_module, monkeypatch):
    app_module._FX_CACHE["quote"] = app_module.FxRateQuote(
        "INR", "USD", 93.0, "cached", app_module.dt.utcnow() - timedelta(days=1)
    )
    monkeypatch.setattr(app_module, "_fx_cache_ttl_seconds", lambda: 1)
    monkeypatch.setattr(app_module, "fetch_inr_per_usd_quote", lambda: (_ for _ in ()).throw(RuntimeError("down")))

    quote = app_module.current_inr_per_usd_quote()

    assert quote.rate == 93.0
    assert quote.stale is True
    assert app_module.convert_inr_to_usd(930, quote) == 10.0


def test_inr_checkout_locks_base_quote_and_does_not_depend_on_live_fx(
    client, app_module, db_session, normal_user, monkeypatch
):
    plan = _paid_plan(app_module)
    plan.plan_amount = 999.0
    plan.offer_price = None
    plan.currency = "INR"
    db_session.commit()
    fake = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", fake)
    monkeypatch.setattr(app_module, "RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setattr(app_module, "fetch_inr_per_usd_quote", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.post("/create-razorpay-order", data={"plan_id": plan.id})
    payload = response.get_json()
    order = app_module.PaymentOrder.query.filter_by(razorpay_order_id="order_fx_fake").first()

    assert payload["success"] is True
    assert fake.order.requests[0]["amount"] == 99900
    assert fake.order.requests[0]["currency"] == "INR"
    assert order.base_amount == 999.0
    assert order.base_currency == "INR"
    assert order.quoted_amount == 999.0
    assert order.quoted_currency == "INR"
    assert order.fx_rate == 1.0
    assert order.fx_rate_source == "canonical-inr-checkout"


def test_existing_order_quote_is_not_recomputed_when_display_rate_changes(app_module, db_session, normal_user, monkeypatch):
    plan = _paid_plan(app_module)
    order = app_module.PaymentOrder(
        order_id="ORD_LOCKED_FX",
        razorpay_order_id="order_locked_fx",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=999.0,
        total_amount=999.0,
        currency="INR",
        base_amount=999.0,
        base_currency="INR",
        quoted_amount=999.0,
        quoted_currency="INR",
        fx_rate=1.0,
        fx_rate_source="canonical-inr-checkout",
        fx_rate_timestamp=app_module.dt.utcnow(),
        status="pending",
    )
    db_session.add(order)
    db_session.commit()
    app_module._FX_CACHE["quote"] = None
    monkeypatch.setattr(
        app_module,
        "fetch_inr_per_usd_quote",
        lambda: app_module.FxRateQuote("INR", "USD", 94.0, "test", app_module.dt.utcnow()),
    )

    assert app_module.convert_inr_to_usd(999) == 10.63
    refreshed = app_module.PaymentOrder.query.get(order.id)
    assert refreshed.quoted_amount == 999.0
    assert refreshed.fx_rate == 1.0


def test_calendar_month_helper_handles_month_end_edges(app_module):
    assert app_module._add_calendar_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert app_module._add_calendar_months(datetime(2024, 1, 31), 1) == datetime(2024, 2, 29)
    assert app_module._add_calendar_months(datetime(2026, 3, 31), 1) == datetime(2026, 4, 30)
    assert app_module._add_calendar_months(datetime(2026, 1, 31), 2) == datetime(2026, 3, 31)


def test_active_and_expired_subscription_extension_use_calendar_month_base(app_module):
    now = datetime(2026, 1, 15, 12, 0, 0)
    assert app_module._extend_subscription_end_calendar_months(datetime(2026, 1, 31), 1, now) == datetime(2026, 2, 28)
    assert app_module._extend_subscription_end_calendar_months(datetime(2026, 1, 1), 3, now) == datetime(2026, 4, 15, 12, 0, 0)


def test_admin_manual_month_extension_no_longer_uses_thirty_days(client, app_module, db_session, admin, normal_user):
    plan = _paid_plan(app_module)
    order = app_module.PaymentOrder(
        order_id="ORD_ADMIN_MONTH",
        razorpay_order_id="order_admin_month",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.effective_price,
        currency="INR",
        status="success",
        subscription_end=datetime(2027, 1, 31),
    )
    db_session.add(order)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    response = client.post(f"/admin/subscriptions/{order.id}/extend", data={"extension_months": "1"})

    assert response.status_code == 302
    assert app_module.PaymentOrder.query.get(order.id).subscription_end == datetime(2027, 2, 28)


def test_trial_registration_anchor_repair_does_not_restart_from_now(app_module, db_session, normal_user):
    anchor = datetime.utcnow() - timedelta(days=20)
    normal_user.subscription_status = "trial"
    normal_user.subscription_taken_at = anchor
    app_module.TrialDetails.query.filter_by(user_id=normal_user.id).delete()
    db_session.commit()
    trial_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
    trial_plan.trial_days = 7
    db_session.commit()

    trial, changed = app_module._repair_missing_trial_details(normal_user, trial_plan)

    assert changed is True
    assert trial.trial_start == anchor
    assert trial.trial_end == anchor + timedelta(days=7)
    assert not trial.is_active


def test_missing_trial_uses_created_at_anchor_when_subscription_taken_at_is_missing(app_module, db_session, normal_user):
    anchor = datetime.utcnow() - timedelta(days=20)
    normal_user.subscription_status = "trial"
    normal_user.subscription_taken_at = None
    normal_user.created_at = anchor
    app_module.TrialDetails.query.filter_by(user_id=normal_user.id).delete()
    db_session.commit()

    trial, changed = app_module._repair_missing_trial_details(normal_user)

    assert changed is True
    assert trial.trial_start == anchor
    assert trial.trial_end == anchor + timedelta(days=7)
    assert not trial.is_active


def test_paid_entitlement_wins_over_stale_trial_state(app_module, db_session, normal_user):
    normal_user.subscription_status = "active"
    normal_user.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    trial = normal_user.trial_details
    trial.trial_end = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    assert normal_user.has_active_subscription() is True
