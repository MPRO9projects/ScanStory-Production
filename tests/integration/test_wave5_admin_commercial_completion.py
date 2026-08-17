"""Wave 5 tests: admin commercial governance completion.

Covers only what Wave 5 actually changed: plan-catalogue governance (validation,
lifecycle, revision, non-destructive delete), lifecycle enforcement at checkout,
add-on type immutability after purchase, admin entitlement grant/revoke for
project capacity and extra scans, governed account-type conversion, refund
operational visibility, coverage/ownership admin inspection and the security
envelope around all of it.

Focused scope by policy: the full suite and the full PostgreSQL certification
lane are the project lead's, run once after this wave merges.
"""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user.id


def _plan_form(**overrides):
    data = {
        "plan_name": "Wave5 Plan",
        "plan_description": "Wave 5 plan",
        "currency": "INR",
        "plan_amount": "999",
        "offer_price": "899",
        "duration_type": "time",
        "duration_value": "12",
        "trial_days": "0",
        "total_project_limit": "5",
        "total_scan_limit": "500",
        "max_pairs_per_project": "10",
        "display_order": "1",
        "plan_family": "INDIVIDUAL",
        "lifecycle_status": "ACTIVE",
        "base_storage_bytes": "1073741824",
        "plan_flags_form": "1",
        "is_active": "on",
        "plan_experience_form": "1",
        "allow_tracked_overlay": "on",
        "allow_detect_once": "on",
        "allow_direct_qr": "on",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _paid_plan(app_module, db_session, **overrides):
    plan = app_module.SubscriptionPlan(
        plan_name=overrides.pop("plan_name", "Paid Plan"),
        plan_amount=499.0,
        currency="INR",
        duration_type="time",
        duration_value=12,
        total_project_limit=5,
        total_scan_limit=500,
        max_pairs_per_project=10,
        is_active=True,
        **overrides,
    )
    db_session.add(plan)
    db_session.commit()
    return plan


def _user(app_module, db_session, email="wave5@example.com", **overrides):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=overrides.pop("project_limit", 5),
        subscribed_scan_limit=overrides.pop("scan_limit", 100),
        projects_used=0,
        scans_used=0,
        **overrides,
    )
    db_session.add(user)
    db_session.commit()
    return user


# ---------------------------------------------------------------------------
# 1. Plan administration
# ---------------------------------------------------------------------------

def test_admin_creates_plan_with_full_commercial_policy(client, app_module, db_session, admin):
    _login_admin(client, admin)
    response = client.post("/admin/plans/add", data=_plan_form(plan_name="Vendor Pro",
                                                              plan_family="BUSINESS_VENDOR",
                                                              allow_direct_qr=None),
                           follow_redirects=False)
    assert response.status_code == 302

    plan = app_module.SubscriptionPlan.query.filter_by(plan_name="Vendor Pro").first()
    assert plan is not None
    # The Wave 2 policy columns are now actually WRITTEN by the admin form.
    assert plan.plan_family == "BUSINESS_VENDOR"
    assert plan.lifecycle_status == "ACTIVE"
    assert plan.base_storage_bytes == 1073741824
    assert plan.allow_direct_qr is False
    assert plan.allow_tracked_overlay is True
    assert plan.plan_revision == 1


@pytest.mark.parametrize(
    "override",
    [
        {"plan_amount": "-1"},
        {"total_project_limit": "-5"},
        {"total_scan_limit": "-1"},
        {"max_pairs_per_project": "0"},
        {"duration_value": "-2"},
        {"trial_days": "-3"},
        {"base_storage_bytes": "-1"},
        {"plan_family": "ENTERPRISE"},
        {"lifecycle_status": "RETIRED"},
        {"duration_type": "forever"},
        {"offer_price": "5000"},
        {"allow_tracked_overlay": None, "allow_detect_once": None, "allow_direct_qr": None},
    ],
)
def test_admin_plan_creation_rejects_invalid_configuration(client, app_module, db_session, admin, override):
    _login_admin(client, admin)
    before = app_module.SubscriptionPlan.query.count()
    response = client.post("/admin/plans/add", data=_plan_form(plan_name="Bad Plan", **override))
    assert response.status_code == 200  # re-rendered form, not a redirect to success
    assert app_module.SubscriptionPlan.query.count() == before


def test_admin_plan_edit_bumps_revision_only_for_commercial_change(client, app_module, db_session, admin):
    _login_admin(client, admin)
    plan = _paid_plan(app_module, db_session, plan_name="Revision Plan")
    assert plan.plan_revision == 1

    client.post(f"/admin/plans/{plan.id}/edit", data=_plan_form(plan_name="Revision Plan",
                                                                total_scan_limit="900"))
    db_session.refresh(plan)
    assert plan.total_scan_limit == 900
    assert plan.plan_revision == 2

    # Presentation-only edit: same commercial policy, no revision bump.
    client.post(f"/admin/plans/{plan.id}/edit", data=_plan_form(plan_name="Revision Plan Renamed",
                                                                total_scan_limit="900"))
    db_session.refresh(plan)
    assert plan.plan_name == "Revision Plan Renamed"
    assert plan.plan_revision == 2


def test_admin_plan_delete_refuses_referenced_plan_and_preserves_payment_snapshot(
    client, app_module, db_session, admin
):
    _login_admin(client, admin)
    plan = _paid_plan(app_module, db_session, plan_name="Referenced Plan")
    user = _user(app_module, db_session, "referenced@example.com")
    snapshot = plan.policy_snapshot()
    order = app_module.PaymentOrder(
        order_id="ORDER_WAVE5_1",
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.plan_amount,
        currency="INR",
        status="success",
        plan_policy_snapshot_json=app_module.json.dumps(snapshot),
    )
    db_session.add(order)
    db_session.commit()
    # Nobody is SUBSCRIBED to it any more - only the payment history references it.
    assert app_module.User.query.filter_by(subscription_id=plan.id).count() == 0

    response = client.post(f"/admin/plans/{plan.id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert app_module.SubscriptionPlan.query.get(plan.id) is not None

    # Editing the live plan afterwards leaves the historical contract untouched.
    client.post(f"/admin/plans/{plan.id}/edit", data=_plan_form(plan_name="Referenced Plan",
                                                                total_project_limit="99"))
    db_session.refresh(order)
    assert app_module.json.loads(order.plan_policy_snapshot_json)["total_project_limit"] == snapshot["total_project_limit"]


def test_admin_plan_delete_allows_unreferenced_plan(client, app_module, db_session, admin):
    _login_admin(client, admin)
    plan = _paid_plan(app_module, db_session, plan_name="Orphan Plan")
    plan_id = plan.id
    client.post(f"/admin/plans/{plan_id}/delete", follow_redirects=True)
    assert app_module.SubscriptionPlan.query.get(plan_id) is None


# ---------------------------------------------------------------------------
# 2. Plan lifecycle governance
# ---------------------------------------------------------------------------

def test_plan_lifecycle_change_is_governed_and_non_destructive(client, app_module, db_session, admin):
    _login_admin(client, admin)
    plan = _paid_plan(app_module, db_session, plan_name="Lifecycle Plan")
    user = _user(app_module, db_session, "lifecycle@example.com", subscription_id=plan.id)
    project = app_module.Project(
        name="Lifecycle Project",
        owner_user_id=user.id,
        created_by_user_id=user.id,
        current_owner_user_id=user.id,
        user_project_index=1,
        qr_code_filename="lifecycle.png",
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    assert client.post(f"/admin/plans/{plan.id}/lifecycle",
                       data={"lifecycle_status": "NONSENSE"}).status_code == 302
    db_session.refresh(plan)
    assert plan.lifecycle_status == "ACTIVE"

    client.post(f"/admin/plans/{plan.id}/lifecycle", data={"lifecycle_status": "CLOSED_FOR_NEW_PURCHASE"})
    db_session.refresh(plan)
    db_session.refresh(user)
    db_session.refresh(project)
    assert plan.lifecycle_status == "CLOSED_FOR_NEW_PURCHASE"
    assert plan.plan_revision == 2
    assert plan.is_purchasable is False
    # Existing subscriber, project and QR are all untouched.
    assert user.subscription_id == plan.id
    assert project.qr_code_filename == "lifecycle.png"
    assert project.is_active is True


def test_non_purchasable_plan_is_hidden_and_rejected_at_checkout(client, app_module, db_session, admin):
    plan = _paid_plan(app_module, db_session, plan_name="Closed Plan",
                      lifecycle_status="CLOSED_FOR_NEW_PURCHASE")
    assert plan.id not in [p.id for p in app_module.purchasable_plans_query().all()]

    user = _user(app_module, db_session, "checkout@example.com")
    _login_user(client, user)
    response = client.post("/create-razorpay-order", data={"plan_id": plan.id})
    assert response.get_json()["success"] is False
    assert app_module.PaymentOrder.query.filter_by(plan_id=plan.id).count() == 0


def test_pending_downgrade_boundary_semantics_unchanged(client, app_module, db_session):
    """Wave 2 mechanism, re-asserted because Wave 5 touched the plan columns."""
    low = _paid_plan(app_module, db_session, plan_name="Low Plan")
    low.total_project_limit = 1
    low.total_scan_limit = 10
    db_session.commit()
    user = _user(app_module, db_session, "downgrade@example.com", project_limit=5, scan_limit=500)
    user.pending_plan_id = low.id
    user.pending_plan_effective_at = datetime.utcnow() + timedelta(days=5)
    db_session.commit()

    assert app_module.apply_pending_plan_change_if_due(user) is False
    assert user.subscribed_project_limit == 5

    user.pending_plan_effective_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert app_module.apply_pending_plan_change_if_due(user) is True
    db_session.refresh(user)
    assert user.subscription_id == low.id
    assert user.subscribed_project_limit == 1
    assert user.pending_plan_id is None


# ---------------------------------------------------------------------------
# 3. Add-on catalogue governance
# ---------------------------------------------------------------------------

def _addon_form(**overrides):
    data = {
        "code": "W5_SCANS",
        "name": "Wave5 Scans",
        "addon_type": "EXTRA_SCANS",
        "unit_amount": "199",
        "currency": "INR",
        "scan_delta": "100",
        "is_active": "on",
        "is_commercially_available": "on",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def test_addon_create_and_invalid_type_rejected(client, app_module, db_session, admin):
    _login_admin(client, admin)
    client.post("/admin/addons/create", data=_addon_form())
    assert app_module.AddonCatalog.query.filter_by(code="W5_SCANS").first() is not None

    client.post("/admin/addons/create", data=_addon_form(code="W5_BAD", addon_type="PAIR_LIMIT"))
    assert app_module.AddonCatalog.query.filter_by(code="W5_BAD").first() is None

    # Type-specific quantity is required; nothing is defaulted.
    client.post("/admin/addons/create", data=_addon_form(code="W5_NOQTY", scan_delta=None))
    assert app_module.AddonCatalog.query.filter_by(code="W5_NOQTY").first() is None


def test_addon_type_is_immutable_once_purchased(client, app_module, db_session, admin):
    _login_admin(client, admin)
    client.post("/admin/addons/create", data=_addon_form(code="W5_LOCK"))
    item = app_module.AddonCatalog.query.filter_by(code="W5_LOCK").first()
    user = _user(app_module, db_session, "addon@example.com")

    # Type is still editable while nothing has been sold.
    client.post(f"/admin/addons/{item.id}/edit",
                data=_addon_form(code="W5_LOCK", addon_type="PROJECT_CAPACITY",
                                 scan_delta=None, project_delta="3"))
    db_session.refresh(item)
    assert item.addon_type == "PROJECT_CAPACITY"

    db_session.add(app_module.AddonPurchase(
        order_id="ADDON_W5_LOCK_1",
        user_id=user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency="INR",
        status="fulfilled",
    ))
    db_session.commit()

    client.post(f"/admin/addons/{item.id}/edit",
                data=_addon_form(code="W5_LOCK", addon_type="EXTRA_SCANS", scan_delta="50"))
    db_session.refresh(item)
    assert item.addon_type == "PROJECT_CAPACITY"

    # Everything else about a sold item stays editable.
    client.post(f"/admin/addons/{item.id}/edit",
                data=_addon_form(code="W5_LOCK", addon_type="PROJECT_CAPACITY",
                                 scan_delta=None, project_delta="3", name="Renamed"))
    db_session.refresh(item)
    assert item.name == "Renamed"


def test_addon_has_no_destructive_delete_route(app_module):
    rules = {str(r) for r in app_module.app.url_map.iter_rules()}
    assert not any("addons" in rule and "delete" in rule for rule in rules)


# ---------------------------------------------------------------------------
# 4. Admin entitlement grants
# ---------------------------------------------------------------------------

def test_admin_project_capacity_grant_and_revoke_are_ledgered(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "capacity@example.com", project_limit=5)

    client.post(f"/admin/users/{user.id}/grant-project-capacity",
                data={"project_slots": "3", "reason": "goodwill"})
    db_session.refresh(user)
    assert user.subscribed_project_limit == 8
    rows = app_module.EntitlementTransaction.query.filter_by(
        user_id=user.id, entitlement_type="PROJECT_CAPACITY").all()
    assert [r.source_type for r in rows] == [app_module._ent.ADMIN_GRANT_SOURCE_TYPE]

    client.post(f"/admin/users/{user.id}/grant-project-capacity",
                data={"project_slots": "-2", "reason": "correction"})
    db_session.refresh(user)
    assert user.subscribed_project_limit == 6
    assert app_module.EntitlementTransaction.query.filter_by(
        user_id=user.id, entitlement_type="PROJECT_CAPACITY").count() == 2

    # Zero is rejected outright rather than writing an empty ledger row.
    client.post(f"/admin/users/{user.id}/grant-project-capacity", data={"project_slots": "0"})
    assert app_module.EntitlementTransaction.query.filter_by(
        user_id=user.id, entitlement_type="PROJECT_CAPACITY").count() == 2


def test_admin_grant_never_overwrites_purchased_entitlement(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "mixed@example.com", scan_limit=100)
    purchased = app_module._apply_entitlement_transaction(
        user, "EXTRA_SCANS", 50, source_type="addon_purchase", source_id=4242,
        reason="purchased scans",
    )[0]
    db_session.commit()
    db_session.refresh(user)
    assert user.subscribed_scan_limit == 150

    client.post(f"/admin/scans/{user.id}/grant-extra", data={"extra_scans": "25"})
    db_session.refresh(user)
    assert user.subscribed_scan_limit == 175

    # Revoking the admin grant leaves the purchased row and its value intact.
    client.post(f"/admin/scans/{user.id}/grant-extra", data={"extra_scans": "-25"})
    db_session.refresh(user)
    assert user.subscribed_scan_limit == 150
    assert app_module.EntitlementTransaction.query.get(purchased.id).delta_value == 50
    assert app_module.EntitlementTransaction.query.filter_by(
        user_id=user.id, source_type="addon_purchase").count() == 1


def test_admin_storage_grant_and_revoke_survive_the_shared_helper(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "storage@example.com")
    client.post(f"/admin/users/{user.id}/grant-storage", data={"storage_bytes": str(2 * 1024 ** 3)})
    client.post(f"/admin/users/{user.id}/grant-storage", data={"storage_bytes": str(-1024 ** 3)})
    rows = app_module.EntitlementTransaction.query.filter_by(
        user_id=user.id, entitlement_type="ACCOUNT_STORAGE").all()
    assert sorted(r.delta_value for r in rows) == [-(1024 ** 3), 2 * 1024 ** 3]
    assert {r.source_type for r in rows} == {app_module._ent.ADMIN_GRANT_SOURCE_TYPE}


def test_entitlement_revocation_clamps_materialized_columns_at_zero(app_module, db_session):
    user = _user(app_module, db_session, "clamp@example.com", project_limit=2, scan_limit=5)
    app_module._apply_entitlement_transaction(
        user, "PROJECT_CAPACITY", -50, source_type="admin_grant", source_id=1, reason="over-revoke")
    app_module._apply_entitlement_transaction(
        user, "EXTRA_SCANS", -50, source_type="admin_grant", source_id=2, reason="over-revoke")
    db_session.commit()
    # Zero, never negative: a negative column would read as "unlimited".
    assert user.subscribed_project_limit == 0
    assert user.subscribed_scan_limit == 0


# ---------------------------------------------------------------------------
# 5. Account-type conversion
# ---------------------------------------------------------------------------

def test_individual_to_vendor_conversion_preserves_everything(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "convert-up@example.com")
    project = app_module.Project(
        name="Convert Project",
        owner_user_id=user.id,
        created_by_user_id=user.id,
        current_owner_user_id=user.id,
        user_project_index=1,
        qr_code_filename="convert.png",
        is_active=True,
    )
    db_session.add(project)
    app_module._apply_entitlement_transaction(
        user, "EXTRA_SCANS", 10, source_type="addon_purchase", source_id=99, reason="paid")
    db_session.commit()
    scan_limit_before = user.subscribed_scan_limit
    subscription_before = user.subscription_id

    client.post(f"/admin/users/{user.id}/account-type",
                data={"account_type": "BUSINESS_VENDOR", "reason": "vendor onboarding"})
    db_session.refresh(user)
    db_session.refresh(project)
    assert user.account_type == "BUSINESS_VENDOR"
    assert user.subscription_id == subscription_before
    assert user.subscribed_scan_limit == scan_limit_before
    assert project.qr_code_filename == "convert.png"
    assert project.current_owner_user_id == user.id
    assert app_module.EntitlementTransaction.query.filter_by(user_id=user.id).count() == 1
    assert app_module.AdminActivity.query.filter_by(activity_type="account_type_change").count() == 1


def test_vendor_to_individual_blocked_by_wave4_dependencies(client, app_module, db_session, admin):
    _login_admin(client, admin)
    vendor = _user(app_module, db_session, "vendor@example.com", account_type="BUSINESS_VENDOR")
    owner = _user(app_module, db_session, "owner@example.com")
    project = app_module.Project(
        name="Vendor Managed",
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        manager_vendor_user_id=vendor.id,
        user_project_index=1,
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    client.post(f"/admin/users/{vendor.id}/account-type", data={"account_type": "INDIVIDUAL"})
    db_session.refresh(vendor)
    db_session.refresh(project)
    assert vendor.account_type == "BUSINESS_VENDOR"
    # The relationship is never silently severed by a blocked conversion.
    assert project.manager_vendor_user_id == vendor.id

    project.manager_vendor_user_id = None
    db_session.commit()
    client.post(f"/admin/users/{vendor.id}/account-type", data={"account_type": "INDIVIDUAL"})
    db_session.refresh(vendor)
    assert vendor.account_type == "INDIVIDUAL"


def test_account_type_conversion_rejects_invalid_value(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "badtype@example.com")
    client.post(f"/admin/users/{user.id}/account-type", data={"account_type": "ENTERPRISE"})
    db_session.refresh(user)
    assert user.account_type == "INDIVIDUAL"


# ---------------------------------------------------------------------------
# 6. Ownership / commercial consistency
# ---------------------------------------------------------------------------

def test_admin_project_view_reports_current_owner_and_preserves_creator(
    client, app_module, db_session, admin
):
    _login_admin(client, admin)
    creator = _user(app_module, db_session, "creator@example.com")
    recipient = _user(app_module, db_session, "recipient@example.com")
    project = app_module.Project(
        name="Transferred Project",
        owner_user_id=creator.id,
        created_by_user_id=creator.id,
        current_owner_user_id=creator.id,
        user_project_index=1,
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    app_module.set_project_current_owner(project, recipient)
    db_session.commit()

    assert app_module.project_current_owner_user_id(project) == recipient.id
    assert app_module.project_created_by_user_id(project) == creator.id

    body = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)
    assert recipient.email in body
    assert creator.email in body


def test_admin_project_view_exposes_coverage_records(client, app_module, db_session, admin):
    _login_admin(client, admin)
    owner = _user(app_module, db_session, "coverage-owner@example.com")
    project = app_module.Project(
        name="Coverage Project",
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=1,
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    coverage = app_module.admin_grant_project_service_coverage(project, admin, 30, "support gesture")
    db_session.commit()

    body = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)
    assert "Service Coverage" in body
    assert "ADMIN_GRANT" in body
    assert "support gesture" in body
    assert coverage.status == "ACTIVE"
    # Coverage grant never touched project, media or QR.
    db_session.refresh(project)
    assert project.is_active is True


def test_admin_coverage_grant_rejects_nonsense_and_is_audited(client, app_module, db_session, admin):
    _login_admin(client, admin)
    owner = _user(app_module, db_session, "coverage-bad@example.com")
    project = app_module.Project(
        name="Coverage Bad", owner_user_id=owner.id, created_by_user_id=owner.id,
        current_owner_user_id=owner.id, user_project_index=1, is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    assert client.post(f"/admin/projects/{project.id}/service-coverage/grant",
                       json={"days": 0, "reason": "x"}).status_code == 400
    assert client.post(f"/admin/projects/{project.id}/service-coverage/grant",
                       json={"days": 30, "reason": ""}).status_code == 400
    assert app_module.ProjectServiceCoverage.query.filter_by(project_id=project.id).count() == 0

    assert client.post(f"/admin/projects/{project.id}/service-coverage/grant",
                       json={"days": 30, "reason": "approved"}).status_code == 201
    assert app_module.AdminActivity.query.filter_by(activity_type="project_coverage_grant").count() == 1


# ---------------------------------------------------------------------------
# 7. Refund operational visibility (scope unchanged: full refunds only)
# ---------------------------------------------------------------------------

def _refund(app_module, db_session, admin, user, **overrides):
    plan = _paid_plan(app_module, db_session, plan_name=f"Refund Plan {overrides.get('suffix', '1')}")
    order = app_module.PaymentOrder(
        order_id=f"ORDER_REF_{overrides.get('suffix', '1')}",
        user_id=user.id,
        plan_id=plan.id,
        amount=100.0,
        total_amount=100.0,
        currency="INR",
        status="success",
        razorpay_payment_id=f"pay_{overrides.get('suffix', '1')}",
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
        reason="operational test",
        requested_by_admin_id=admin.id,
        idempotency_key=f"refund:payment_order:{order.id}",
    )
    db_session.add(refund)
    db_session.commit()
    return refund


def test_admin_refund_list_surfaces_failed_reconciliation(client, app_module, db_session, admin):
    _login_admin(client, admin)
    user = _user(app_module, db_session, "refund@example.com")
    healthy = _refund(app_module, db_session, admin, user, suffix="1")
    broken = _refund(app_module, db_session, admin, user, suffix="2",
                     reconciliation_status="MANUAL_REVIEW_REQUIRED")

    payload = client.get("/admin/api/refunds").get_json()
    assert payload["success"] is True
    assert {r["id"] for r in payload["refunds"]} == {healthy.id, broken.id}

    attention = client.get("/admin/api/refunds?needs_attention=1").get_json()
    assert [r["id"] for r in attention["refunds"]] == [broken.id]

    filtered = client.get("/admin/api/refunds?reconciliation_status=APPLIED").get_json()
    assert [r["id"] for r in filtered["refunds"]] == [healthy.id]

    assert client.get("/admin/api/refunds?status=NOPE").status_code == 400


def test_refund_list_is_read_only(app_module):
    rule = next(r for r in app_module.app.url_map.iter_rules() if str(r) == "/admin/api/refunds")
    assert rule.methods & {"POST", "PUT", "DELETE", "PATCH"} == set()


# ---------------------------------------------------------------------------
# 8. Security envelope
# ---------------------------------------------------------------------------

WAVE5_MUTATION_ROUTES = [
    "/admin/plans/add",
    "/admin/plans/<int:plan_id>/edit",
    "/admin/plans/<int:plan_id>/delete",
    "/admin/plans/<int:plan_id>/lifecycle",
    "/admin/users/<int:user_id>/grant-project-capacity",
    "/admin/users/<int:user_id>/grant-storage",
    "/admin/users/<int:user_id>/account-type",
]


def test_no_wave5_commercial_mutation_is_reachable_by_get(app_module):
    rules = {str(r): r for r in app_module.app.url_map.iter_rules()}
    for path in WAVE5_MUTATION_ROUTES:
        rule = rules[path]
        assert "POST" in rule.methods
        if path.endswith("/add") or path.endswith("/edit"):
            continue  # GET renders the form only
        assert "GET" not in rule.methods


def test_wave5_commercial_mutations_require_csrf(app_module, admin, db_session):
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        with app_module.app.test_client() as csrf_client:
            _login_admin(csrf_client, admin)
            plan = _paid_plan(app_module, db_session, plan_name="CSRF Plan")
            response = csrf_client.post(f"/admin/plans/{plan.id}/lifecycle",
                                        data={"lifecycle_status": "ARCHIVED"})
            assert response.status_code == 400
            db_session.refresh(plan)
            assert plan.lifecycle_status == "ACTIVE"
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = False


def test_plan_and_addon_governance_requires_superadmin(client, app_module, db_session, secondary_admin):
    _login_admin(client, secondary_admin)  # role="admin", not superadmin
    plan = _paid_plan(app_module, db_session, plan_name="Guarded Plan")
    for path, data in (
        ("/admin/plans/add", _plan_form(plan_name="Sneaky")),
        (f"/admin/plans/{plan.id}/lifecycle", {"lifecycle_status": "ARCHIVED"}),
        ("/admin/addons/create", _addon_form(code="W5_SNEAK")),
    ):
        assert client.post(path, data=data).status_code == 302
    db_session.refresh(plan)
    assert plan.lifecycle_status == "ACTIVE"
    assert app_module.SubscriptionPlan.query.filter_by(plan_name="Sneaky").first() is None
    assert app_module.AddonCatalog.query.filter_by(code="W5_SNEAK").first() is None


def test_logged_in_user_cannot_invoke_admin_commercial_actions(client, app_module, db_session):
    user = _user(app_module, db_session, "intruder@example.com")
    victim = _user(app_module, db_session, "victim@example.com", project_limit=5)
    _login_user(client, user)
    for path, data in (
        ("/admin/plans/add", _plan_form(plan_name="User Plan")),
        (f"/admin/users/{victim.id}/grant-project-capacity", {"project_slots": "100"}),
        (f"/admin/users/{victim.id}/account-type", {"account_type": "BUSINESS_VENDOR"}),
    ):
        assert client.post(path, data=data).status_code in (302, 401, 403)
    db_session.refresh(victim)
    assert victim.subscribed_project_limit == 5
    assert victim.account_type == "INDIVIDUAL"
    assert app_module.SubscriptionPlan.query.filter_by(plan_name="User Plan").first() is None
