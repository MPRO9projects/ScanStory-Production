"""Targeted validation for V1 Agent 2's backend<->frontend parity fixes:
admin capacity UI (task 2), webhook-events admin page (task 3), the three
previously-orphaned project/plan admin actions (task 4), per-user scan
controls (task 5), settings-page honesty (task 6), and the styled
unavailable-project response (task 7). Task 1 (admin password reset email
crash) is covered separately in tests/security/test_otp_security.py.

These are route/template-level smoke tests only - no scanner CV/recognition
code is touched or exercised here.
"""
import pytest


# ---------------------------------------------------------------------------
# Task 2: Capacity admin UI
# ---------------------------------------------------------------------------

def test_admin_capacity_page_shows_snapshot(client, login_admin, app_module):
    response = client.get("/admin/capacity")
    assert response.status_code == 200
    assert b"Configured Limit" in response.data
    assert b"Consumed" in response.data


def test_admin_capacity_update_persists_limit_and_enabled(client, login_admin, app_module, db_session):
    response = client.post("/admin/capacity", data={"configured_limit": "42", "enabled": "on"})
    assert response.status_code == 302
    config = app_module.CapacityConfig.query.get(1)
    assert config.configured_limit == 42
    assert config.enabled is True

    # Unchecking the box (browser omits the field entirely) must persist disabled.
    response = client.post("/admin/capacity", data={"configured_limit": "42"})
    assert response.status_code == 302
    db_session.expire_all()
    config = app_module.CapacityConfig.query.get(1)
    assert config.enabled is False


def test_admin_capacity_rejects_non_positive_limit(client, login_admin, app_module):
    app_module._get_or_create_capacity_config()
    response = client.post("/admin/capacity", data={"configured_limit": "0", "enabled": "on"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"must be a positive integer" in response.data
    config = app_module.CapacityConfig.query.get(1)
    assert config.configured_limit != 0


def test_admin_capacity_never_exposes_consumed_count_as_an_input(client, login_admin):
    response = client.get("/admin/capacity")
    assert b'name="consumed_count"' not in response.data


def test_admin_capacity_requires_capacity_permission(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/capacity", follow_redirects=True)
    assert response.status_code == 200
    assert b"Access denied" in response.data


# ---------------------------------------------------------------------------
# Task 3: Webhook events (read-only)
# ---------------------------------------------------------------------------

def _make_webhook_event(app_module, db_session, **overrides):
    defaults = dict(
        idempotency_key=f"payment.captured|pay_test|order_test_{overrides.get('event_type', 'x')}",
        event_type="payment.captured",
        razorpay_payment_id="pay_test123",
        razorpay_order_id="order_test123",
        payload_hash="deadbeef" * 8,
        processing_status="processed",
    )
    defaults.update(overrides)
    event = app_module.RazorpayWebhookEvent(**defaults)
    db_session.add(event)
    db_session.commit()
    return event


def test_admin_webhook_events_page_lists_events_read_only(client, login_admin, app_module, db_session):
    _make_webhook_event(app_module, db_session)
    response = client.get("/admin/webhook-events")
    assert response.status_code == 200
    assert b"payment.captured" in response.data
    assert b"Processed" in response.data
    # Never displayed: raw payload / signature / secret material.
    assert b"deadbeef" not in response.data
    assert b"signature" not in response.data.lower()


def test_admin_webhook_events_no_mutation_route_exposed(client, login_admin):
    # Read-only page: no replay/delete form action anywhere in the body.
    response = client.get("/admin/webhook-events")
    assert b'action="/admin/webhook-events' not in response.data
    assert b"replay" not in response.data.lower()


def test_admin_webhook_events_filters_by_order(client, login_admin, app_module, db_session, normal_user):
    plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    order = app_module.PaymentOrder(
        user_id=normal_user.id, plan=plan, order_id="filter-order-1",
        amount=100, total_amount=100, currency="INR", status="success",
    )
    db_session.add(order)
    db_session.commit()
    matching = _make_webhook_event(app_module, db_session, event_type="matching", payment_order_id=order.id,
                                    idempotency_key="matching-key")
    _make_webhook_event(app_module, db_session, event_type="other", idempotency_key="other-key")

    response = client.get(f"/admin/webhook-events?order_id={order.id}")
    assert response.status_code == 200
    assert str(matching.id).encode() in response.data


def test_view_payment_page_links_to_webhook_history(client, login_admin, app_module, db_session, normal_user):
    plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    payment = app_module.PaymentOrder(
        user_id=normal_user.id, plan=plan, order_id="linked-order-1",
        amount=100, total_amount=100, currency="INR", status="success",
        payment_at=app_module.dt.utcnow(),
    )
    db_session.add(payment)
    db_session.commit()
    response = client.get(f"/admin/payments/{payment.id}")
    assert response.status_code == 200
    assert f"/admin/webhook-events?order_id={payment.id}".encode() in response.data


# ---------------------------------------------------------------------------
# Task 4: orphaned project/plan actions
# ---------------------------------------------------------------------------

def test_admin_view_project_has_delete_button_for_superadmin(client, login_admin, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/admin/projects/{project.id}")
    assert response.status_code == 200
    assert f'action="/admin/projects/{project.id}/delete"'.encode() in response.data


def test_admin_delete_project_route_works_from_ui_form(client, login_admin, app_module, db_session, project_with_pair):
    project, _pair = project_with_pair
    project_id = project.id
    response = client.post(f"/admin/projects/{project_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert app_module.Project.query.get(project_id) is None


def test_admin_plans_page_has_toggle_status_button(client, login_admin, app_module, db_session):
    plan = app_module.SubscriptionPlan(
        plan_name="Toggle Test Plan", plan_amount=199, duration_type="time", duration_value=1,
        total_project_limit=3, total_scan_limit=30, max_pairs_per_project=1, is_active=True,
    )
    db_session.add(plan)
    db_session.commit()
    response = client.get("/admin/plans")
    assert response.status_code == 200
    assert f'action="/admin/plans/{plan.id}/toggle-status"'.encode() in response.data
    assert b"Deactivate" in response.data


def test_admin_toggle_plan_status_route_works_from_ui_form(client, login_admin, app_module, db_session):
    plan = app_module.SubscriptionPlan(
        plan_name="Toggle Test Plan 2", plan_amount=199, duration_type="time", duration_value=1,
        total_project_limit=3, total_scan_limit=30, max_pairs_per_project=1, is_active=True,
    )
    db_session.add(plan)
    db_session.commit()
    response = client.post(f"/admin/plans/{plan.id}/toggle-status", follow_redirects=True)
    assert response.status_code == 200
    db_session.expire_all()
    assert app_module.SubscriptionPlan.query.get(plan.id).is_active is False


# ---------------------------------------------------------------------------
# Task 5: per-user scan controls
# ---------------------------------------------------------------------------

def test_user_scans_page_has_scan_management_controls(client, login_admin, normal_user):
    response = client.get(f"/admin/scans/user/{normal_user.id}")
    assert response.status_code == 200
    assert f'action="/admin/scans/{normal_user.id}/update-limit"'.encode() in response.data
    assert f'action="/admin/scans/{normal_user.id}/grant-extra"'.encode() in response.data
    assert f'action="/admin/scans/{normal_user.id}/lock-scanner"'.encode() in response.data


def test_admin_update_scan_limit_via_form_field_name(client, login_admin, app_module, db_session, normal_user):
    response = client.post(f"/admin/scans/{normal_user.id}/update-limit", data={"new_scan_limit": "500"},
                            follow_redirects=True)
    assert response.status_code == 200
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).subscribed_scan_limit == 500


def test_admin_grant_extra_scans_via_form_field_name(client, login_admin, app_module, db_session, normal_user):
    before = normal_user.subscribed_scan_limit
    response = client.post(f"/admin/scans/{normal_user.id}/grant-extra", data={"extra_scans": "10"},
                            follow_redirects=True)
    assert response.status_code == 200
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).subscribed_scan_limit == before + 10


def test_admin_lock_user_scanner_via_form(client, login_admin, app_module, db_session, normal_user):
    response = client.post(f"/admin/scans/{normal_user.id}/lock-scanner", follow_redirects=True)
    assert response.status_code == 200
    db_session.expire_all()
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.subscription_status == "limit_reached"
    assert refreshed.scans_used == refreshed.subscribed_scan_limit


# ---------------------------------------------------------------------------
# Task 6: settings page honesty
# ---------------------------------------------------------------------------

def test_admin_settings_dead_fields_are_disabled(client, login_admin):
    response = client.get("/admin/settings")
    assert response.status_code == 200
    body = response.data.decode()
    assert '<input type="text" id="site_name" class="form-control"' in body
    site_name_tag = body.split('id="site_name"', 1)[1].split(">", 1)[0]
    assert "disabled" in site_name_tag
    assert "Not active in V1" in body


def test_admin_settings_trial_fields_still_editable_and_persisted(client, login_admin, app_module):
    response = client.post("/admin/settings", data={
        "free_trial_projects": "3",
        "free_trial_scans": "77",
        "free_trial_days": "14",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert app_module.get_system_config("free_trial_projects", None) == 3
    assert app_module.get_system_config("free_trial_scans", None) == 77
    assert app_module.get_system_config("free_trial_days", None) == 14


def test_admin_settings_post_does_not_write_dead_fields(client, login_admin, app_module):
    client.post("/admin/settings", data={
        "free_trial_projects": "1", "free_trial_scans": "50", "free_trial_days": "7",
        "site_name": "Attempted Override", "maintenance_mode": "on",
    }, follow_redirects=True)
    # Disabled inputs mean browsers never send these keys anyway, but even if a raw
    # POST includes them, the route must not persist them - they're not read anywhere.
    config = app_module.SystemConfig.query.filter_by(config_key="site_name").first()
    assert config is None or config.config_value != "Attempted Override"


# ---------------------------------------------------------------------------
# Task 7: styled unavailable-project response
# ---------------------------------------------------------------------------

def test_scanner_page_for_suspended_project_returns_styled_404(client, app_module, db_session, project_with_pair):
    project, _pair = project_with_pair
    project.is_active = False
    db_session.commit()
    response = client.get(f"/scanner/{project.id}")
    assert response.status_code == 404
    body = response.data.decode()
    assert "suspended or unavailable" in body
    assert "SCANSTORY" in body
    assert "<html" in body.lower()  # styled page, not the old bare-text tuple body
