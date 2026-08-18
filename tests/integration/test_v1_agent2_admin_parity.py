"""Targeted validation for V1 Agent 2's backend<->frontend parity fixes:
admin capacity UI (task 2), webhook-events admin page (task 3), the three
previously-orphaned project/plan admin actions (task 4), per-user scan
controls (task 5), settings-page honesty (task 6), and the styled
unavailable-project response (task 7). Task 1 (admin password reset email
crash) is covered separately in tests/security/test_otp_security.py.

These are route/template-level smoke tests only - no scanner CV/recognition
code is touched or exercised here.
"""
from datetime import datetime, timedelta
import json

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


def test_view_payment_page_does_not_dump_raw_gateway_payload(client, login_admin, app_module, db_session, normal_user):
    plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    payment = app_module.PaymentOrder(
        user_id=normal_user.id, plan=plan, order_id="privacy-order-1",
        amount=100, total_amount=100, currency="INR", status="success",
        razorpay_order_id="order_visible_safe", razorpay_payment_id="pay_visible_safe",
        payment_at=app_module.dt.utcnow(),
    )
    db_session.add(payment)
    db_session.commit()

    response = client.get(f"/admin/payments/{payment.id}")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Gateway Diagnostics" in body
    assert "order_visible_safe" in body
    assert "pay_visible_safe" in body
    template_source = open("templates/admin/view_payment.html", encoding="utf-8").read()
    assert "payment.payment_details|tojson|safe" not in template_source
    assert "Raw Payment Response" not in template_source


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
    assert b"Existing subscriptions are not deleted" in response.data


def test_admin_edit_plan_warns_about_live_entitlement_impact(client, login_admin, app_module, db_session):
    plan = app_module.SubscriptionPlan(
        plan_name="Impact Warning Plan", plan_amount=199, duration_type="time", duration_value=1,
        total_project_limit=3, total_scan_limit=30, max_pairs_per_project=2, is_active=True,
    )
    db_session.add(plan)
    db_session.commit()

    response = client.get(f"/admin/plans/{plan.id}/edit")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Live plan impact" in body
    assert "Project and scan limits are materialized" in body
    assert "pairs-per-project is read live" in body


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
    assert "Read-only" in body
    assert "managed by server" in body
    assert "Not active in V1" not in body
    assert 'id="generalForm"' not in body
    assert 'id="paymentForm"' not in body
    assert 'id="securityForm"' not in body
    assert 'role="group" aria-label="General settings read-only"' in body


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


def test_standalone_admin_sidebar_exposes_current_navigation_for_superadmin(client, login_admin):
    response = client.get("/admin/payments")
    assert response.status_code == 200
    body = response.data.decode()
    for label in (
        "Dashboard", "Users", "Projects", "Content Reports", "Scans",
        "Plans", "Subscriptions", "Payments", "Admin Management",
        "Capacity", "Operations", "Settings", "Activity Logs",
    ):
        assert f"<span>{label}</span>" in body


def test_admin_json_fetch_sites_use_resilient_non_json_helper():
    for path in (
        "templates/admin/moderation.html",
        "templates/admin/operations.html",
        "templates/admin/view_payment.html",
    ):
        html = open(path, encoding="utf-8").read()
        assert 'include "admin/_admin_fetch_helper.html"' in html
        assert "parseAdminJsonResponse(response)" in html

    helper = open("templates/admin/_admin_fetch_helper.html", encoding="utf-8").read()
    assert "Security token expired. Refresh the page and try again." in helper
    assert "Your admin session has expired. Please sign in again." in helper


def test_operations_page_distinguishes_configured_from_healthy(client, login_admin):
    response = client.get("/admin/operations")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Processing service" in body
    assert "Not verified" in body or "Online" in body or "Unreachable" in body
    assert "not proof that a worker is actually running" in body
    assert "not proof that mail is arriving" in body
    # Infrastructure vocabulary must not leak back into the operator UI.
    for leaked in ("Redis", "RQ /", "SMTP", "Queue ID", "usable_worker_count"):
        assert leaked not in body


def test_destructive_admin_copy_distinguishes_suspend_delete_deactivate(client, login_admin, secondary_admin, project_with_pair):
    project, _pair = project_with_pair
    project_response = client.get(f"/admin/projects/{project.id}")
    assert project_response.status_code == 200
    project_body = project_response.data.decode()
    assert "payments are not deleted or refunded" in project_body
    assert "This is not a suspension and cannot be undone" in project_body

    admins_response = client.get("/admin/admins")
    assert admins_response.status_code == 200
    admins_body = admins_response.data.decode()
    assert "does not delete audit history" in admins_body
    assert "Use deactivate for routine access suspension" in admins_body


# ---------------------------------------------------------------------------
# V1.1 Wave 2: Commercial UX/admin presentation
# ---------------------------------------------------------------------------

def test_pricing_page_shows_account_family_and_experience_contract_without_fake_plan_family(
    client, app_module, db_session
):
    """The pricing page must render the plan's REAL Wave 2 entitlement fields
    (plan_family, allow_* experience flags, base_storage_bytes, media policy) -
    not a hardcoded family list and not the pre-Wave-2 'backend pending' copy.
    """
    vendor_plan = app_module.SubscriptionPlan(
        plan_name="Vendor Direct QR Plan", plan_amount=999, duration_type="time", duration_value=1,
        total_project_limit=10, total_scan_limit=1000, max_pairs_per_project=3, is_active=True,
        plan_family="BUSINESS_VENDOR",
        allow_direct_qr=True, allow_detect_once=False, allow_tracked_overlay=False,
        base_storage_bytes=5 * 1024 * 1024 * 1024,
    )
    db_session.add(vendor_plan)
    db_session.commit()

    response = client.get("/pricing")
    assert response.status_code == 200
    body = response.data.decode()

    assert "V1.1 Account Families" in body
    assert "Individual" in body
    assert "Business / Vendor" in body
    assert "Direct QR" in body
    assert "Detect Once" in body
    assert "Tracked Overlay" in body
    assert "Object Tracking" not in body

    # Real per-plan experience entitlements: this plan allows ONLY Direct QR,
    # so the other two must render as explicitly not included.
    assert "Tracked Overlay, Detect Once" in body
    assert "Not included" in body
    # Real per-file media policy + real base storage ENTITLEMENT.
    assert "Image up to 50 MB; video up to 1 GB" in body
    assert "5 GB" in body
    # Wave 3 storage accounting is real now; pricing copy must be non-destructive.
    assert "Storage allowance is tracked against account media usage" in body
    assert "media and QR codes are not deleted" in body

    # The pre-Wave-2 placeholders must be gone now that the fields are real.
    assert "Backend plan-family fields are not available" not in body
    assert "Backend pending" not in body
    assert "without inventing family-specific limits" not in body


def test_pricing_upgrade_downgrade_copy_is_non_destructive(client):
    response = client.get("/pricing")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Upgrades take effect after confirmed payment" in body
    assert "Downgrades are scheduled for the next plan-term boundary" in body
    assert "do not delete existing projects" in body
    assert "Changes take effect immediately" not in body


def test_admin_plan_pages_expose_policy_contract_without_unbacked_inputs(client, login_admin, plan):
    for path in ("/admin/plans", "/admin/plans/add", f"/admin/plans/{plan.id}/edit"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.data.decode()
        assert "policy" in body.lower()
        assert "V1.1" not in body
        assert "Direct QR" in body
        assert "Detect Once" in body
        assert "Tracked Overlay" in body
        assert "Object Tracking" not in body
        # The fields exist on SubscriptionPlan now, so the stale placeholder
        # must be gone.
        assert "Backend pending" not in body

        # Still unbacked: there is no single field behind any of these names.
        for forbidden_input in (
            'name="base_storage"',
            'name="media_policy"',
            'name="experience_entitlements"',
            'name="revision_status"',
        ):
            assert forbidden_input not in body

    # Wave 5 gave plan_family and lifecycle_status real, validated admin forms;
    # every input below is backed by a SubscriptionPlan column and a server-side
    # validator, so they are no longer forbidden.
    for path in ("/admin/plans/add", f"/admin/plans/{plan.id}/edit"):
        form_body = client.get(path).data.decode()
        for backed_input in (
            'name="plan_family"',
            'name="lifecycle_status"',
            'name="base_storage_bytes"',
            'name="max_image_bytes"',
            'name="allow_tracked_overlay"',
        ):
            assert backed_input in form_body

    # The plans list renders each plan's REAL policy fields.
    listing = client.get("/admin/plans").data.decode()
    assert "Individual" in listing
    assert "Live (editable on add/edit)" in listing
    assert "ACTIVE / rev 1" in listing
    assert "Image 50 MB; video 1 GB" in listing


def test_user_profile_entitlement_summary_uses_backend_ledgers(
    client, app_module, db_session, login_user
):
    user = login_user
    # A BUSINESS_VENDOR plan on an INDIVIDUAL account: the page must show the
    # plan's real plan_family, which proves it is not an account_type proxy.
    plan = app_module.SubscriptionPlan(
        plan_name="Profile Entitlement Plan", plan_amount=499, duration_type="time", duration_value=1,
        total_project_limit=5, total_scan_limit=100, max_pairs_per_project=2, is_active=True,
        plan_family="BUSINESS_VENDOR",
        allow_direct_qr=True, allow_detect_once=True, allow_tracked_overlay=False,
        base_storage_bytes=2 * 1024 * 1024 * 1024,
    )
    db_session.add(plan)
    db_session.commit()
    user.subscription_id = plan.id
    user.subscription_status = "active"
    user.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    user.subscribed_project_limit = 7
    user.subscribed_scan_limit = 120
    user.projects_used = 3
    user.scans_used = 4
    db_session.add_all([
        app_module.EntitlementTransaction(
            user_id=user.id,
            entitlement_type="PROJECT_CAPACITY",
            delta_value=2,
            source_type="test",
            source_id=2001,
            reason="profile summary test",
        ),
        app_module.EntitlementTransaction(
            user_id=user.id,
            entitlement_type="EXTRA_SCANS",
            delta_value=20,
            source_type="test",
            source_id=2002,
            reason="profile summary test",
        ),
    ])
    db_session.commit()

    response = client.get("/profile")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Effective Entitlement" in body
    # Base vs purchased capacity stays distinguishable, never one opaque total.
    assert "Project Slots" in body
    assert "Plan slots" in body
    assert "Purchased slots" in body

    # Real plan_family from the backend resolver, distinct from account_type.
    assert "Business / Vendor" in body
    assert "Individual" in body  # the account type, still rendered separately

    # Real experience entitlement flags, per mode.
    assert "Direct QR — Included" in body
    assert "Detect Once — Included" in body
    assert "Tracked Overlay — Not included" in body

    # Real per-file media policy and base storage ENTITLEMENT from the resolver.
    assert "image up to 50 MB" in body
    assert "video up to 1 GB" in body
    assert "Storage allowance:" not in body
    assert "Base storage" in body
    assert "Effective total" in body
    assert "2 GB" in body
    # Wave 3 delivered usage accounting, so the "not measured yet" disclaimer
    # this page carried through Wave 2 is now obsolete and must not render.
    assert "Storage usage is not measured yet" not in body

    assert "Backend pending" not in body


def test_profile_shows_real_storage_breakdown_and_account_storage_addons(
    client, app_module, db_session, login_user
):
    user = login_user
    gb = 1024 * 1024 * 1024
    plan = app_module.SubscriptionPlan(
        plan_name="Storage UX Plan", plan_amount=499, duration_type="time", duration_value=1,
        total_project_limit=5, total_scan_limit=100, max_pairs_per_project=2, is_active=True,
        base_storage_bytes=5 * gb,
    )
    addon = app_module.AddonCatalog(
        code="STORAGE_2GB", name="2 GB storage pack", description="Adds account storage",
        addon_type="ACCOUNT_STORAGE", unit_amount=199, currency="INR", storage_bytes_delta=2 * gb,
        is_active=True, is_commercially_available=True,
    )
    db_session.add_all([plan, addon])
    db_session.commit()
    user.subscription_id = plan.id
    user.subscription_status = "active"
    user.storage_used_bytes = 6 * gb
    db_session.add_all([
        app_module.EntitlementTransaction(
            user_id=user.id, entitlement_type="ACCOUNT_STORAGE", delta_value=2 * gb,
            source_type="addon_purchase", source_id=9101, reason="storage UX test",
        ),
        app_module.EntitlementTransaction(
            user_id=user.id, entitlement_type="ACCOUNT_STORAGE", delta_value=1 * gb,
            source_type="admin_grant", source_id=9102, reason="storage UX test",
        ),
    ])
    db_session.commit()

    response = client.get("/profile")
    assert response.status_code == 200
    body = response.data.decode()

    assert 'data-testid="storage-entitlement-summary"' in body
    assert "Base storage" in body
    assert "Purchased storage" in body
    assert "Admin grant" in body
    assert "Effective total" in body
    assert "Used" in body
    assert "Remaining" in body
    assert "5 GB" in body
    assert "6 GB" in body
    assert "8 GB" in body
    assert "2 GB" in body
    assert "1 GB" in body
    assert 'addonType: \'ACCOUNT_STORAGE\'' in body
    assert "Storage usage is not measured yet" not in body


def test_profile_over_storage_copy_is_truthful_and_non_destructive(
    client, app_module, db_session, login_user
):
    user = login_user
    gb = 1024 * 1024 * 1024
    plan = app_module.SubscriptionPlan(
        plan_name="Small Storage Plan", plan_amount=199, duration_type="time", duration_value=1,
        total_project_limit=5, total_scan_limit=100, max_pairs_per_project=2, is_active=True,
        base_storage_bytes=1 * gb,
    )
    db_session.add(plan)
    db_session.commit()
    user.subscription_id = plan.id
    user.subscription_status = "active"
    user.storage_used_bytes = 3 * gb
    db_session.commit()

    response = client.get("/profile")
    assert response.status_code == 200
    body = response.data.decode()

    assert 'data-testid="storage-overage-copy"' in body
    assert "over storage by" in body
    assert "2 GB" in body
    assert "Existing projects, media and QR codes remain available" in body
    assert "New uploads that consume more storage are blocked" in body
    assert "Smaller replacements may be allowed" in body
    assert "automatically deleted" not in body


def test_admin_user_views_show_backend_sourced_entitlement_and_grandfathering_copy(
    client, login_admin, app_module, db_session, normal_user
):
    normal_user.subscription_status = "active"
    normal_user.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    normal_user.subscribed_project_limit = 3
    normal_user.subscribed_scan_limit = 10
    normal_user.projects_used = 5
    normal_user.scans_used = 12
    db_session.add(app_module.EntitlementTransaction(
        user_id=normal_user.id,
        entitlement_type="PROJECT_CAPACITY",
        delta_value=1,
        source_type="test",
        source_id=3001,
        reason="admin summary test",
    ))
    db_session.commit()

    for path in (f"/admin/users/{normal_user.id}", f"/admin/users/{normal_user.id}/dashboard"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.data.decode()
        assert "Effective Entitlement" in body
        assert "Plan 2 + purchased 1" in body
        assert "Existing projects are retained" in body
        assert "Direct QR" in body
        assert "Detect Once" in body
        assert "Tracked Overlay" in body
        assert "Storage Used" in body or "Storage Used / Remaining" in body
        assert "storage usage is not tracked yet" not in body


def test_admin_user_page_exposes_storage_grant_without_path_or_secret_leak(
    client, login_admin, app_module, db_session, normal_user
):
    gb = 1024 * 1024 * 1024
    normal_user.storage_used_bytes = 3 * gb
    plan = app_module.SubscriptionPlan(
        plan_name="Admin Storage Plan", plan_amount=199, duration_type="time", duration_value=1,
        total_project_limit=5, total_scan_limit=100, max_pairs_per_project=2, is_active=True,
        base_storage_bytes=1 * gb,
    )
    db_session.add(plan)
    db_session.commit()
    normal_user.subscription_id = plan.id
    db_session.commit()

    response = client.get(f"/admin/users/{normal_user.id}")
    assert response.status_code == 200
    body = response.data.decode()

    assert 'data-testid="admin-storage-grant-form"' in body
    assert f"/admin/users/{normal_user.id}/grant-storage" in body
    assert 'name="storage_bytes"' in body
    assert "Revocation never deletes media or QR codes" in body
    assert "Existing projects, media and QR codes remain available" in body
    assert "F:\\\\" not in body
    assert "SECRET_KEY" not in body


# ---------------------------------------------------------------------------
# V1.1 Wave 4: vendor / ownership / transfer / claim presentation foundation
# ---------------------------------------------------------------------------

def _user(app_module, db_session, email, first_name, account_type=None):
    user = app_module.User(
        email=email,
        first_name=first_name,
        last_name="User",
        password_hash="test-only",
        is_verified=True,
        subscription_status="trial",
        account_type=account_type or app_module.ACCOUNT_TYPE_INDIVIDUAL,
        subscribed_project_limit=10,
        subscribed_scan_limit=100,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_self_project_preview_keeps_ownership_panel_simple(
    client, login_user, project_with_pair
):
    project, _pair = project_with_pair
    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Current owner" in body
    assert "Creator" not in body
    assert "Managing Vendor" not in body
    assert "Customer" not in body


def test_vendor_managed_project_preview_distinguishes_creator_owner_manager_and_customer(
    client, login_user, app_module, db_session, project_with_pair
):
    owner = login_user
    project, _pair = project_with_pair
    vendor = _user(app_module, db_session, "vendor@example.com", "Vendor", app_module.ACCOUNT_TYPE_BUSINESS_VENDOR)
    customer = _user(app_module, db_session, "customer@example.com", "Customer")
    project.created_by_user_id = vendor.id
    project.current_owner_user_id = owner.id
    project.manager_vendor_user_id = vendor.id
    project.beneficiary_user_id = customer.id
    db_session.commit()

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Creator" in body
    assert "Vendor User" in body
    assert "Current owner" in body
    assert "Managed by" in body
    assert "Customer" in body
    assert "Customer User" in body
    assert "you now own this project" not in body.lower()


def test_pending_capacity_transfer_copy_is_non_destructive_and_capacity_truthful(
    client, login_user, app_module, db_session, project_with_pair
):
    project, _pair = project_with_pair
    recipient = _user(app_module, db_session, "recipient@example.com", "Recipient")
    project.current_owner_user_id = login_user.id
    transfer = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=login_user.id,
        from_owner_user_id=login_user.id,
        to_user_id=recipient.id,
        status="PENDING_CAPACITY",
    )
    db_session.add(transfer)
    db_session.commit()

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()
    normalized = " ".join(body.split())

    assert "Recipient needs project/storage capacity" in body
    assert "project" in body.lower()
    assert "storage capacity" in body.lower()
    assert "Ownership has not changed" in body
    assert "media and QR code remain intact" in body
    assert "the current owner stays authoritative" in normalized
    assert "deleted" not in body.lower()


def test_claim_presentation_does_not_imply_ownership_changed(
    client, login_user, app_module, db_session, project_with_pair
):
    project, _pair = project_with_pair
    claimant = _user(app_module, db_session, "claimant@example.com", "Claimant")
    project.current_owner_user_id = login_user.id
    claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=claimant.id,
        current_owner_user_id=login_user.id,
        status="APPROVED_BY_ADMIN",
        evidence_summary="Customer says this is theirs",
    )
    db_session.add(claim)
    db_session.commit()

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Ownership review requests" in body
    assert "Approved - ownership handover started" in body
    assert "Submitting one never moves a ScanStory on its own" in body
    assert "you now own this project" not in body.lower()


def test_project_preview_links_to_real_ownership_center_and_claim_route(
    client, app_module, db_session, project_with_pair
):
    project, _pair = project_with_pair
    owner = app_module.User.query.get(project.owner_user_id)
    vendor = _user(app_module, db_session, "claiming-vendor@example.com", "Claiming Vendor", app_module.ACCOUNT_TYPE_BUSINESS_VENDOR)
    project.current_owner_user_id = owner.id
    project.manager_vendor_user_id = vendor.id
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = vendor.id

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert 'href="/ownership"' in body
    assert f'action="/projects/{project.id}/ownership-claim"' in body
    assert 'name="evidence_summary"' in body
    assert "Submitting a claim does not transfer ownership" in body
    assert "not something you can start from here yet" not in body


def test_project_preview_hides_duplicate_claim_submission_for_active_claim(
    client, app_module, db_session, project_with_pair
):
    project, _pair = project_with_pair
    owner = app_module.User.query.get(project.owner_user_id)
    vendor = _user(app_module, db_session, "active-claim-vendor@example.com", "Active Claim Vendor", app_module.ACCOUNT_TYPE_BUSINESS_VENDOR)
    project.current_owner_user_id = owner.id
    project.manager_vendor_user_id = vendor.id
    claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=vendor.id,
        current_owner_user_id=owner.id,
        status="OPEN",
    )
    db_session.add(claim)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = vendor.id

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert f'action="/projects/{project.id}/ownership-claim"' not in body
    assert "You already have an active ownership review request" in body


def test_user_ownership_page_wires_real_transfer_actions_and_capacity_copy(
    client, app_module, db_session, normal_user, project_with_pair
):
    project, _pair = project_with_pair
    sender = _user(app_module, db_session, "handover-sender@example.com", "Handover Sender")
    outgoing_recipient = _user(app_module, db_session, "handover-recipient@example.com", "Handover Recipient")
    pending_project = app_module.Project(name="Waiting Transfer", owner_user_id=sender.id, current_owner_user_id=sender.id)
    blocked_project = app_module.Project(name="Blocked Transfer", owner_user_id=sender.id, current_owner_user_id=sender.id)
    outgoing_project = app_module.Project(name="Outgoing Transfer", owner_user_id=normal_user.id, current_owner_user_id=normal_user.id)
    transferable_project = app_module.Project(name="Available Transfer", owner_user_id=normal_user.id, current_owner_user_id=normal_user.id)
    db_session.add_all([pending_project, blocked_project, outgoing_project, transferable_project])
    db_session.flush()
    pending = app_module.ProjectOwnershipTransfer(
        project_id=pending_project.id,
        initiated_by_user_id=sender.id,
        from_owner_user_id=sender.id,
        to_user_id=normal_user.id,
        status="PENDING_ACCEPTANCE",
    )
    blocked = app_module.ProjectOwnershipTransfer(
        project_id=blocked_project.id,
        initiated_by_user_id=sender.id,
        from_owner_user_id=sender.id,
        to_user_id=normal_user.id,
        status="PENDING_CAPACITY",
        metadata_json=json.dumps({
            "capacity_block": {
                "storage_ok": False,
                "project_slot_ok": False,
                "project_bytes": 1234,
                "checked_at": "2026-01-01T00:00:00",
            }
        }),
    )
    outgoing = app_module.ProjectOwnershipTransfer(
        project_id=outgoing_project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=outgoing_recipient.id,
        status="PENDING_ACCEPTANCE",
    )
    completed = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=outgoing_recipient.id,
        status="COMPLETED",
    )
    db_session.add_all([pending, blocked, outgoing, completed])
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.get("/ownership")
    assert response.status_code == 200
    body = response.data.decode()

    assert f'action="/ownership/transfers/{pending.id}/accept"' in body
    assert f'action="/ownership/transfers/{pending.id}/reject"' in body
    assert f'action="/ownership/transfers/{blocked.id}/retry"' in body
    assert "Capacity still needed" in body
    assert "storage" in body
    assert "project slot" in body
    assert "Ownership has not changed" in body
    assert "media plus QR remain intact" in body
    assert "This handover is under manual ScanStory review" not in body
    assert f'action="/ownership/transfers/{outgoing.id}/cancel"' in body
    assert f'action="/projects/{transferable_project.id}/transfer"' in body
    assert 'name="recipient_email"' in body
    assert 'name="retain_vendor_management"' in body
    assert 'name="reason"' in body
    assert f"/ownership/transfers/{completed.id}/" not in body


def test_user_navigation_exposes_ownership_center(
    client, login_user
):
    pages = ["/dashboard", "/projects", "/profile"]

    for path in pages:
        response = client.get(path)
        assert response.status_code == 200
        body = response.data.decode()
        assert 'href="/ownership"' in body
        assert "Ownership" in body


def test_ownership_center_disputed_state_is_review_only(
    client, app_module, db_session, normal_user
):
    sender = _user(app_module, db_session, "dispute-sender@example.com", "Dispute Sender")
    project = app_module.Project(name="Disputed Handover", owner_user_id=sender.id, current_owner_user_id=sender.id)
    db_session.add(project)
    db_session.flush()
    disputed = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=sender.id,
        from_owner_user_id=sender.id,
        to_user_id=normal_user.id,
        status="DISPUTED",
    )
    db_session.add(disputed)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.get("/ownership")
    assert response.status_code == 200
    body = response.data.decode()

    assert "under manual ScanStory review" in body
    assert f'action="/ownership/transfers/{disputed.id}/accept"' not in body
    assert f'action="/ownership/transfers/{disputed.id}/reject"' not in body
    assert f'action="/ownership/transfers/{disputed.id}/retry"' not in body


def test_user_ownership_page_wires_claim_response_and_cancellation(
    client, app_module, db_session, normal_user, project_with_pair
):
    project, _pair = project_with_pair
    claimant = _user(app_module, db_session, "claim-response@example.com", "Claim Response")
    owned_project = app_module.Project(name="Claimed Story", owner_user_id=normal_user.id, current_owner_user_id=normal_user.id)
    other_owner = _user(app_module, db_session, "other-owner@example.com", "Other Owner")
    claimed_project = app_module.Project(name="My Claim", owner_user_id=other_owner.id, current_owner_user_id=other_owner.id)
    db_session.add_all([owned_project, claimed_project])
    db_session.flush()
    incoming_claim = app_module.ProjectOwnershipClaim(
        project_id=owned_project.id,
        claimant_user_id=claimant.id,
        current_owner_user_id=normal_user.id,
        status="OPEN",
        evidence_summary="Customer proof",
    )
    my_claim = app_module.ProjectOwnershipClaim(
        project_id=claimed_project.id,
        claimant_user_id=normal_user.id,
        current_owner_user_id=other_owner.id,
        status="PENDING_ADMIN_REVIEW",
    )
    completed_claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=normal_user.id,
        current_owner_user_id=other_owner.id,
        status="TRANSFER_COMPLETED",
    )
    db_session.add_all([incoming_claim, my_claim, completed_claim])
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.get("/ownership")
    assert response.status_code == 200
    body = response.data.decode()

    assert f'action="/ownership/claims/{incoming_claim.id}/respond"' in body
    assert 'name="decision" value="accept"' in body
    assert 'name="decision" value="refuse"' in body
    assert 'name="note"' in body
    assert "Refusing moves the request to Admin review" in body
    assert f'action="/ownership/claims/{my_claim.id}/cancel"' in body
    assert "The linked handover completed. Ownership is now transferred." in body
    assert f'action="/ownership/claims/{completed_claim.id}/cancel"' not in body


def test_admin_project_view_links_to_real_ownership_review(
    client, login_admin, app_module, db_session, normal_user, project_with_pair
):
    project, _pair = project_with_pair
    vendor = _user(app_module, db_session, "admin-vendor@example.com", "Admin Vendor", app_module.ACCOUNT_TYPE_BUSINESS_VENDOR)
    customer = _user(app_module, db_session, "admin-customer@example.com", "Admin Customer")
    recipient = _user(app_module, db_session, "admin-recipient@example.com", "Admin Recipient")
    project.created_by_user_id = vendor.id
    project.current_owner_user_id = normal_user.id
    project.manager_vendor_user_id = vendor.id
    project.beneficiary_user_id = customer.id
    transfer = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=recipient.id,
        retain_vendor_management=True,
        status="PENDING_CAPACITY",
    )
    claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=recipient.id,
        current_owner_user_id=normal_user.id,
        status="PENDING_ADMIN_REVIEW",
    )
    db_session.add_all([transfer, claim])
    db_session.commit()

    response = client.get(f"/admin/projects/{project.id}")
    assert response.status_code == 200
    body = response.data.decode()

    assert 'data-testid="admin-ownership-context"' in body
    assert "Creator" in body
    assert "Current Owner" in body
    assert "Managing Vendor" in body
    assert "Customer / Beneficiary" in body
    assert "PENDING CAPACITY" in body
    assert "project and/or storage capacity" in body
    assert "Claims do not transfer ownership by themselves" in body
    assert "Coverage/service state is separate from ownership state" in body
    assert 'href="/admin/ownership"' in body
    assert f'action="/admin/projects/{project.id}/service-coverage/grant"' in body
    assert 'id="coverageGrantDays"' in body
    assert 'id="coverageGrantReason"' in body
    assert "Grant service coverage to this project?" in body
    assert "Coverage could not be granted" in body
    assert "backend action routes are available" not in body
    assert "F:\\\\" not in body
    assert "SECRET_KEY" not in body


def test_admin_ownership_page_wires_state_aware_real_actions(
    client, login_admin, app_module, db_session, normal_user
):
    recipient = _user(app_module, db_session, "admin-action-recipient@example.com", "Admin Action Recipient")
    project = app_module.Project(name="Admin Ownership Queue", owner_user_id=normal_user.id, current_owner_user_id=normal_user.id)
    db_session.add(project)
    db_session.flush()
    pending = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=recipient.id,
        status="PENDING_ACCEPTANCE",
    )
    disputed = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=recipient.id,
        status="DISPUTED",
    )
    completed = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=normal_user.id,
        from_owner_user_id=normal_user.id,
        to_user_id=recipient.id,
        status="COMPLETED",
    )
    claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=recipient.id,
        current_owner_user_id=normal_user.id,
        status="PENDING_ADMIN_REVIEW",
        evidence_summary="Needs review",
    )
    terminal_claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=recipient.id,
        current_owner_user_id=normal_user.id,
        status="REJECTED",
    )
    db_session.add_all([pending, disputed, completed, claim, terminal_claim])
    db_session.commit()

    response = client.get("/admin/ownership")
    assert response.status_code == 200
    body = response.data.decode()

    assert f'action="/admin/ownership/transfers/{pending.id}/complete"' in body
    assert f'action="/admin/ownership/transfers/{pending.id}/dispute"' in body
    assert f'action="/admin/ownership/transfers/{pending.id}/cancel"' in body
    assert f'action="/admin/ownership/transfers/{disputed.id}/release-dispute"' in body
    assert "ownership-affecting admin action" in body
    assert "Approval opens a governed handover" in body
    assert "The current owner remains unchanged" in body
    assert f'action="/admin/ownership/transfers/{completed.id}/complete"' not in body
    assert f'action="/admin/ownership/claims/{claim.id}/approve"' in body
    assert f'action="/admin/ownership/claims/{claim.id}/reject"' in body
    assert f'action="/admin/ownership/claims/{terminal_claim.id}/approve"' not in body
    assert 'name="decision_reason"' in body
    assert 'name="reason"' in body


def test_project_preview_expired_coverage_is_distinct_from_suspension(
    client, login_user, app_module, db_session, project_with_pair
):
    project, _pair = project_with_pair
    project.is_active = True
    normal_user = login_user
    normal_user.subscription_status = "expired"
    normal_user.subscription_expires_at = datetime.utcnow() - timedelta(days=2)
    db_session.commit()

    response = client.get(f"/project/{project.id}/preview")
    assert response.status_code == 200
    body = response.data.decode()

    assert "Coverage expired" in body
    assert "public viewer is not live because coverage has expired" in body
    assert "project, QR code and media have not been deleted" in body
    assert "suspended after a review" not in body


def test_admin_templates_include_mobile_table_hardening():
    base = open("templates/admin/base.html", encoding="utf-8").read()
    view_project = open("templates/admin/view_project.html", encoding="utf-8").read()
    addons = open("templates/admin/addons.html", encoding="utf-8").read()

    assert ".table-responsive" in base
    assert ".table-container" in base
    assert "min-width: 760px" in base
    assert "-webkit-overflow-scrolling: touch" in view_project
    assert "min-width: 760px" in view_project
    assert ".table-scroll table" in addons
    assert "min-width: 860px" in addons
