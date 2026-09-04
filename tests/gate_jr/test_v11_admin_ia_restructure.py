"""SCANSTORY V1.1 - World-Class Admin Portal Restructure (2026-09-02).

Focused pack for the IA restructure only. Run:
    python -m pytest tests/gate_jr/test_v11_admin_ia_restructure.py -q

Covers: ownership-scoped project lists (My/All Admin/Customer Projects),
sidebar visibility for Admin vs Super Admin, the Scans rewrite, Commercial
visibility (Add-ons/Subscriptions/Payments), and the ownership-invalid-state
defense. Does not re-run the full suite.
"""
import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Fixtures: a clean multi-owner project set, matching the brief's own QA
# scenario (Admin A owns A1/A2, Admin B owns B1, two customers each own one).
# ---------------------------------------------------------------------------

@pytest.fixture()
def second_admin(app_module, db_session, admin):
    other = app_module.Admin(
        email="admin-b-ia@example.com",
        name="Admin B",
        password_hash=generate_password_hash("AdminBPass123"),
        role="admin",
        is_active=True,
        created_by=admin.id,
    )
    db_session.add(other)
    db_session.commit()
    return other


@pytest.fixture()
def second_customer(app_module, db_session):
    user = app_module.User(
        email="ia-customer-2@example.com",
        first_name="Customer",
        last_name="Two",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def project_matrix(app_module, db_session, admin, second_admin, normal_user, second_customer):
    def _mk(name, **kw):
        p = app_module.Project(name=name, **kw)
        db_session.add(p)
        return p

    a1 = _mk("A1 (bootstrap admin)", owner_admin_id=admin.id)
    a2 = _mk("A2 (bootstrap admin)", owner_admin_id=admin.id)
    b1 = _mk("B1 (second admin)", owner_admin_id=second_admin.id)
    u1p = _mk("U1P (normal_user)", owner_user_id=normal_user.id, current_owner_user_id=normal_user.id)
    u2p = _mk("U2P (second_customer)", owner_user_id=second_customer.id, current_owner_user_id=second_customer.id)
    db_session.commit()
    return {"a1": a1, "a2": a2, "b1": b1, "u1p": u1p, "u2p": u2p}


# ---------------------------------------------------------------------------
# §52 - Project IA scoping
# ---------------------------------------------------------------------------

def test_my_projects_shows_only_own_admin_owned_projects(client, login_admin, project_matrix):
    resp = client.get("/admin/my-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert project_matrix["a1"].name in body
    assert project_matrix["a2"].name in body
    assert project_matrix["b1"].name not in body
    assert project_matrix["u1p"].name not in body
    assert project_matrix["u2p"].name not in body


def test_regular_admin_cannot_access_all_admin_projects(client, secondary_admin, project_matrix):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    resp = client.get("/admin/admin-projects", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/").endswith("/admin/dashboard")


def test_superadmin_all_admin_projects_shows_every_admin_owned_project_only(client, login_admin, project_matrix):
    resp = client.get("/admin/admin-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert project_matrix["a1"].name in body
    assert project_matrix["a2"].name in body
    assert project_matrix["b1"].name in body
    assert project_matrix["u1p"].name not in body
    assert project_matrix["u2p"].name not in body


def test_customer_projects_shows_only_customer_owned_projects(client, login_admin, project_matrix):
    resp = client.get("/admin/customer-projects")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert project_matrix["u1p"].name in body
    assert project_matrix["u2p"].name in body
    assert project_matrix["a1"].name not in body
    assert project_matrix["b1"].name not in body


def test_my_projects_never_shows_all_admin_projects_even_for_superadmin(client, login_admin, project_matrix):
    """My Projects is always self-scoped - a superadmin viewing their OWN
    workspace must never see another admin's projects folded in."""
    resp = client.get("/admin/my-projects")
    body = resp.data.decode()
    assert project_matrix["b1"].name not in body


def test_all_admin_projects_edit_button_only_shown_for_own_rows(client, login_admin, second_admin, project_matrix):
    resp = client.get("/admin/admin-projects")
    body = resp.data.decode()
    a1_row = body.split(f'>{project_matrix["a1"].name}<')[1].split("</tr>")[0]
    b1_row = body.split(f'>{project_matrix["b1"].name}<')[1].split("</tr>")[0]
    assert f'/projects/{project_matrix["a1"].id}/edit' in a1_row
    assert f'/projects/{project_matrix["b1"].id}/edit' not in b1_row


# ---------------------------------------------------------------------------
# §53 - Sidebar visibility
# ---------------------------------------------------------------------------

def test_regular_admin_sidebar_visibility(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    resp = client.get("/admin/dashboard")
    body = resp.data.decode()
    for present in ("Dashboard", "My Projects", "Create Project", "Customers",
                    "Customer Projects", "Scans", "Content Reports",
                    "Ownership Review", "Payments"):
        assert present in body, present
    for absent in ("All Admin Projects", "Plans", "Add-ons", "Subscriptions",
                    "Admin Management", "Signup Capacity",
                    "Activity Logs"):
        assert absent not in body, absent
    # "Operations"/"Settings" as bare substrings also match the "Customer
    # Operations" section heading / unrelated copy - check the actual hrefs.
    assert 'href="/admin/operations"' not in body
    assert 'href="/admin/settings"' not in body


def test_superadmin_sidebar_visibility(client, login_admin):
    resp = client.get("/admin/dashboard")
    body = resp.data.decode()
    for present in ("Dashboard", "My Projects", "All Admin Projects", "Create Project",
                    "Customers", "Customer Projects", "Scans", "Content Reports",
                    "Ownership Review", "Plans", "Add-ons", "Subscriptions", "Payments",
                    "Admin Management", "Signup Capacity", "Operations", "Settings",
                    "Activity Logs"):
        assert present in body, present


# ---------------------------------------------------------------------------
# §54 - Scans fix
# ---------------------------------------------------------------------------

def test_admin_scans_renders_200_with_real_data(client, login_admin, app_module, db_session, project_with_pair, normal_user):
    project, pair = project_with_pair
    success = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="ia-scan-1", is_successful=True, counted=True,
    )
    failed = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="ia-scan-2", is_successful=False, counted=True,
    )
    db_session.add_all([success, failed])
    db_session.commit()

    resp = client.get("/admin/scans")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Customer Usage" in body
    assert "Recent Activity" in body
    assert normal_user.email in body
    assert project.name in body
    # Dead controls removed, not left inert.
    assert "scan_type" not in body
    assert 'name="search"' not in body


def test_admin_scans_date_and_customer_filters_apply(client, login_admin, app_module, db_session, project_with_pair, normal_user):
    project, pair = project_with_pair
    scan = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="ia-scan-filter", is_successful=True, counted=True,
    )
    db_session.add(scan)
    db_session.commit()

    hit = client.get(f"/admin/scans?user_id={normal_user.id}")
    assert hit.status_code == 200
    assert normal_user.email in hit.data.decode()

    miss = client.get("/admin/scans?user_id=999999")
    assert miss.status_code == 200
    assert "No scan activity found" in miss.data.decode()


def test_user_scans_no_longer_has_dead_scan_type_filter(client, login_admin, normal_user):
    resp = client.get(f"/admin/scans/user/{normal_user.id}")
    assert resp.status_code == 200
    assert 'name="scan_type"' not in resp.data.decode()


# ---------------------------------------------------------------------------
# §55 - Commercial visibility
# ---------------------------------------------------------------------------

def test_addons_hidden_from_regular_admin_visible_to_superadmin(client, secondary_admin, login_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    hidden = client.get("/admin/dashboard").data.decode()
    assert "Add-ons" not in hidden

    resp = client.get("/admin/addons")
    assert resp.status_code == 302  # regular admin denied

    visible = client.get("/admin/dashboard")
    with client.session_transaction() as sess:
        sess["admin_id"] = login_admin.id
    visible = client.get("/admin/dashboard").data.decode()
    assert "Add-ons" in visible
    assert client.get("/admin/addons").status_code == 200


def test_payments_visible_to_both_roles_refunds_reachable_from_payments(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    resp = client.get("/admin/payments")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'href="/admin/payments/refunds"' in body
    assert 'href="/admin/webhook-events"' in body
    assert client.get("/admin/payments/refunds").status_code == 200


def test_webhook_events_has_no_top_level_sidebar_entry(client, login_admin):
    body = client.get("/admin/dashboard").data.decode()
    assert 'href="/admin/webhook-events"' not in body


def test_subscriptions_increase_limits_form_present(client, login_admin, app_module, db_session, normal_user):
    plan = app_module.SubscriptionPlan(
        plan_name="IA Restructure Plan", plan_amount=100, duration_type="time",
        duration_value=1, total_project_limit=3, total_scan_limit=30, is_active=True,
    )
    payment = app_module.PaymentOrder(
        user_id=normal_user.id, plan=plan, order_id="ia-sub-order-1", amount=100,
        total_amount=100, currency="INR", status="success",
        payment_at=app_module.dt.utcnow(), subscription_start=app_module.dt.utcnow(),
        subscription_end=app_module.dt.utcnow() + app_module.timedelta(days=30),
    )
    db_session.add_all([plan, payment])
    db_session.commit()

    resp = client.get("/admin/subscriptions")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f'action="/admin/subscriptions/{payment.id}/increase-limits"' in body
    assert 'name="additional_projects"' in body
    assert 'name="additional_scans"' in body


# ---------------------------------------------------------------------------
# §56 - Platform
# ---------------------------------------------------------------------------

def test_signup_capacity_label_and_backend_intact(client, login_admin):
    resp = client.get("/admin/capacity")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Signup Capacity" in body
    assert 'name="configured_limit"' in body
    assert 'name="enabled"' in body


def test_operations_no_longer_hosts_primary_refund_action(client, login_admin):
    resp = client.get("/admin/operations")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "addon-refund-btn" not in body
    assert 'href="/admin/payments/refunds"' in body


# ---------------------------------------------------------------------------
# Ownership invalid-state defense
# ---------------------------------------------------------------------------

def test_project_with_both_owners_set_is_flagged_invalid_and_edit_blocked(client, login_admin, app_module, db_session, normal_user):
    project = app_module.Project(
        name="Invalid Ownership Project",
        owner_user_id=normal_user.id,
        owner_admin_id=login_admin.id,
    )
    db_session.add(project)
    db_session.commit()

    assert app_module.project_ownership_state(project) == "both"

    resp = client.get(f"/projects/{project.id}/edit", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Ownership Error" in resp.data

    list_resp = client.get("/admin/admin-projects")
    assert b"Ownership Error" in list_resp.data


def test_project_with_neither_owner_set_is_flagged_invalid(app_module, db_session):
    project = app_module.Project(name="Orphan Project")
    db_session.add(project)
    db_session.commit()
    assert app_module.project_ownership_state(project) == "neither"


def test_project_with_exactly_one_owner_is_valid(app_module, db_session, normal_user, admin):
    user_owned = app_module.Project(name="Valid User Project", owner_user_id=normal_user.id)
    admin_owned = app_module.Project(name="Valid Admin Project", owner_admin_id=admin.id)
    db_session.add_all([user_owned, admin_owned])
    db_session.commit()
    assert app_module.project_ownership_state(user_owned) == "valid"
    assert app_module.project_ownership_state(admin_owned) == "valid"


# ---------------------------------------------------------------------------
# Preflight parity fix
# ---------------------------------------------------------------------------

def test_admin_can_reach_validate_target_and_validate_video_for_own_project(
    client, login_admin, app_module, db_session
):
    project = app_module.Project(name="Admin Preflight Project", owner_admin_id=login_admin.id)
    db_session.add(project)
    db_session.commit()

    resp = client.post(
        f"/projects/{project.id}/media/validate-video",
        json={"video_hash": "a" * 64, "pair_index": "new"},
    )
    # Must not 404 (the pre-fix regression) - a real JSON verdict comes back.
    assert resp.status_code == 200
    assert resp.get_json()["verdict"] in ("UNIQUE", "CONFLICT")
