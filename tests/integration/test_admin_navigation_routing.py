"""fix/admin-navigation-routing.

Root causes fixed:
1. Dashboard redirect: /dashboard was gated by @login_required, which only
   ever checks session["user_id"]. An authenticated Admin/Super Admin has
   session["admin_id"] instead, so any @login_required route (and the
   normal-user /login/ page) treated them as fully anonymous and sent them
   to the user login page instead of their real admin dashboard.
2. Projects/Admin Projects flicker: admin/base.html's sidebar had two
   competing top-level project links ("My Projects" -> admin_my_projects,
   "User Projects" -> admin_projects) pointing at two different routes for
   what is conceptually the same "admin project management" destination.
"""
import pytest


# ---------------------------------------------------------------------------
# 1-3: Admin/Super Admin dashboard access
# ---------------------------------------------------------------------------

def test_admin_session_can_open_admin_dashboard(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/dashboard")
    assert response.status_code == 200


def test_superadmin_session_can_open_admin_dashboard(client, login_admin):
    response = client.get("/admin/dashboard")
    assert response.status_code == 200


def test_admin_dashboard_does_not_redirect_to_admin_login_when_authenticated(client, login_admin):
    response = client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code == 200
    assert response.status_code != 302


# ---------------------------------------------------------------------------
# 4-5: normal user dashboard access / admin isolation
# ---------------------------------------------------------------------------

def test_normal_user_can_open_dashboard(client, login_user):
    response = client.get("/dashboard")
    assert response.status_code == 200


def test_normal_user_cannot_open_admin_dashboard(client, login_user):
    response = client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# 6-7: unauthenticated behavior
# ---------------------------------------------------------------------------

def test_unauthenticated_admin_dashboard_redirects_to_admin_login(client):
    response = client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_unauthenticated_dashboard_follows_normal_user_login_flow(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert "/admin/login" not in response.headers["Location"]


# ---------------------------------------------------------------------------
# Root-cause regression: an existing Admin/Super Admin session must never be
# bounced to the normal-user login page (this was the literal reported bug -
# "clicking Dashboard can redirect to a login page").
# ---------------------------------------------------------------------------

def test_admin_session_hitting_generic_dashboard_goes_to_admin_dashboard_not_login(client, login_admin):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/admin/dashboard")


def test_admin_can_open_selected_user_dashboard_context_without_impersonation(client, login_admin, normal_user):
    response = client.get(f"/admin/users/{normal_user.id}/dashboard")
    assert response.status_code == 200
    assert b"User Dashboard Context" in response.data
    assert b"you are not impersonating this user" in response.data
    assert normal_user.email.encode() in response.data
    with client.session_transaction() as sess:
        assert sess.get("admin_id") == login_admin.id
        assert sess.get("user_id") is None


def test_admin_user_dashboard_context_return_is_audited(client, login_admin, normal_user, app_module):
    response = client.get(f"/admin/users/{normal_user.id}/dashboard/return", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/admin/users/{normal_user.id}")
    assert app_module.AdminActivity.query.filter_by(
        admin_id=login_admin.id,
        activity_type="exit_user_dashboard",
    ).count() == 1


def test_admin_session_visiting_user_login_page_is_redirected_away(client, login_admin):
    response = client.get("/login/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/admin/dashboard")


# ---------------------------------------------------------------------------
# 8-9: canonical Admin Projects navigation + stable active-menu state
# ---------------------------------------------------------------------------

def test_admin_nav_has_one_canonical_project_entry(client, login_admin):
    response = client.get("/admin/dashboard")
    data = response.data
    assert b"User Projects" not in data
    assert b"My Projects" not in data
    assert data.count(b'href="/admin/projects"') == 1


def test_admin_nav_exposes_users_scans_subscriptions_and_activity_logs_once(client, login_admin):
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    data = response.data
    for href in (
        b'href="/admin/users"',
        b'href="/admin/scans"',
        b'href="/admin/subscriptions"',
        b'href="/admin/activity-logs"',
    ):
        assert href in data
    assert b"Admin Management" in data
    # "User Profiles" was consolidated into the single canonical Users nav
    # entry (the dashboard's separate "Recent Users -> View All" shortcut
    # also links to /admin/users, so this checks the sidebar item specifically
    # rather than every href on the page).
    assert data.count(b'<i class="fas fa-users"></i> Users') == 1
    assert b"User Profiles" not in data
    assert b'href="/admin/user-profiles"' not in data


def test_project_list_page_shows_correct_active_nav_state(client, login_admin):
    response = client.get("/admin/projects")
    assert response.status_code == 200
    assert b'href="/admin/projects" class="sidebar-link active"' in response.data


def test_project_detail_page_shows_correct_active_nav_state(client, login_admin, app_module, db_session):
    project = app_module.Project(name="Admin Owned Project", owner_admin_id=login_admin.id)
    db_session.add(project)
    db_session.commit()
    response = client.get(f"/admin/projects/{project.id}")
    assert response.status_code == 200
    assert b'href="/admin/projects" class="sidebar-link active"' in response.data


# ---------------------------------------------------------------------------
# 10: legacy project route redirects directly to the canonical route
# ---------------------------------------------------------------------------

def test_legacy_my_projects_route_redirects_to_canonical_admin_projects(client, login_admin):
    response = client.get("/admin/my-projects", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/admin/projects")


def test_admin_user_profiles_redirects_to_canonical_admin_users(client, login_admin, normal_user):
    response = client.get(
        f"/admin/user-profiles?search={normal_user.email}&status=active",
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/admin/users")
    assert "status=active" in location
    assert f"search={normal_user.email}".encode() in location.encode() or normal_user.email in location

    followed = client.get(f"/admin/user-profiles?search={normal_user.email}", follow_redirects=True)
    assert followed.status_code == 200
    assert normal_user.email.encode() in followed.data
    assert b"User Profiles" not in followed.data


def test_view_user_detail_exposes_view_user_dashboard_action(client, login_admin, normal_user):
    # The one action admin/user-profiles.html had that admin/users.html
    # lacked ("View User Dashboard") now lives on the user-detail page.
    response = client.get(f"/admin/users/{normal_user.id}")
    assert response.status_code == 200
    assert b"View User Dashboard" in response.data
    assert f"/admin/users/{normal_user.id}/dashboard".encode() in response.data


def test_user_scans_links_use_existing_route_and_delete_is_disabled(client, login_admin, normal_user):
    response = client.get(f"/admin/scans/user/{normal_user.id}")
    assert response.status_code == 200
    assert f"/admin/scans/user/{normal_user.id}".encode() in response.data
    assert f"/admin/users/{normal_user.id}/scans".encode() not in response.data
    assert b"/delete" not in response.data
    assert b"Delete unavailable" in response.data or b"No scan logs found" in response.data


# ---------------------------------------------------------------------------
# admin_user_scans: templates/admin/user_scans.html reads scan_logs/status/
# search, but the route never passed them - Jinja's lenient Undefined
# silently rendered an always-empty table instead of crashing. These tests
# prove real rows now reach the template (a 200 with real ScanLog data only
# happens if every field the template touches per-row resolves, including
# arithmetic Undefined can't support), that the status tabs really filter on
# ScanLog.is_successful, and that search really filters on the project name.
# ---------------------------------------------------------------------------

def test_admin_user_scans_renders_real_rows_and_status_defaults_to_all(
    client, login_admin, app_module, db_session, project_with_pair, normal_user
):
    project, pair = project_with_pair
    success = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="scan-success-1", is_successful=True, counted=True,
    )
    failed = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="scan-failed-1", is_successful=False, counted=True,
    )
    db_session.add_all([success, failed])
    db_session.commit()

    response = client.get(f"/admin/scans/user/{normal_user.id}")
    assert response.status_code == 200
    body = response.data.decode()

    # Real data actually flowed through (not an empty/undefined table).
    assert body.count(project.name) >= 2
    assert '<span class="badge badge-success">Success</span>' in body
    assert '<span class="badge badge-danger">Failed</span>' in body
    assert "No scan logs found for this user" not in body

    # Default status is "all" and its tab is marked active.
    assert 'class="status-tab active">All Scans</a>' in body

    # Agent 2's scan-control block is untouched and still renders.
    assert "Agent 2 (task 5): scan-management controls - begin" in body
    assert "Agent 2 (task 5): scan-management controls - end" in body


def test_admin_user_scans_status_filter_uses_is_successful(
    client, login_admin, app_module, db_session, project_with_pair, normal_user
):
    project, pair = project_with_pair
    success = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="scan-success-2", is_successful=True, counted=True,
    )
    failed = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="scan-failed-2", is_successful=False, counted=True,
    )
    db_session.add_all([success, failed])
    db_session.commit()

    only_success = client.get(f"/admin/scans/user/{normal_user.id}?status=success")
    assert only_success.status_code == 200
    body = only_success.data.decode()
    assert '<span class="badge badge-success">Success</span>' in body
    assert '<span class="badge badge-danger">Failed</span>' not in body

    only_failed = client.get(f"/admin/scans/user/{normal_user.id}?status=failed")
    body = only_failed.data.decode()
    assert '<span class="badge badge-danger">Failed</span>' in body
    assert '<span class="badge badge-success">Success</span>' not in body

    # "partial" has no ScanLog equivalent - must yield zero rows, not
    # silently fall back to showing everything.
    partial = client.get(f"/admin/scans/user/{normal_user.id}?status=partial")
    assert partial.status_code == 200
    assert "No scan logs found for this user" in partial.data.decode()


def test_admin_user_scans_search_filters_by_real_project_name(
    client, login_admin, app_module, db_session, project_with_pair, normal_user
):
    project, pair = project_with_pair
    scan = app_module.ScanLog(
        project_id=project.id, pair_id=pair.id, user_id=normal_user.id,
        scan_session_id="scan-search-1", is_successful=True, counted=True,
    )
    db_session.add(scan)
    db_session.commit()

    hit = client.get(f"/admin/scans/user/{normal_user.id}?search={project.name}")
    assert hit.status_code == 200
    assert project.name in hit.data.decode()

    miss = client.get(f"/admin/scans/user/{normal_user.id}?search=NoSuchProjectXYZ")
    assert miss.status_code == 200
    assert "No scan logs found for this user" in miss.data.decode()


def test_payment_detail_subscription_action_uses_subscription_list(client, login_admin, app_module, db_session, normal_user):
    plan = app_module.SubscriptionPlan(
        plan_name="Navigation Test Plan",
        plan_amount=100,
        duration_type="time",
        duration_value=1,
        total_project_limit=3,
        total_scan_limit=30,
        is_active=True,
    )
    payment = app_module.PaymentOrder(
        user_id=normal_user.id,
        plan=plan,
        order_id="nav-order-1",
        amount=100,
        total_amount=100,
        currency="INR",
        status="success",
        payment_at=app_module.dt.utcnow(),
        subscription_start=app_module.dt.utcnow(),
        subscription_end=app_module.dt.utcnow() + app_module.timedelta(days=30),
    )
    db_session.add_all([plan, payment])
    db_session.commit()

    response = client.get(f"/admin/payments/{payment.id}")
    assert response.status_code == 200
    assert b"/admin/subscriptions/" not in response.data
    assert b"/admin/subscriptions?search=" in response.data
    assert b"View User's Subscriptions" in response.data


# ---------------------------------------------------------------------------
# P1 fix: /admin/capacity existed with no sidebar entry anywhere in the admin
# UI. Nav link is gated on the same 'superadmin.capacity.manage' permission
# the route itself enforces (see require_admin_permission in app.py), using
# the exact admin_can()/request.endpoint pattern already used for the
# adjacent "Admin Management" link.
# ---------------------------------------------------------------------------

def test_admin_nav_shows_capacity_link_with_active_state_for_superadmin(client, login_admin):
    response = client.get("/admin/capacity")
    assert response.status_code == 200
    body = response.data.decode()
    assert 'href="/admin/capacity"' in body
    assert '<a class="nav-link active" aria-current="page" href="/admin/capacity">' in body


def test_admin_nav_hides_capacity_link_for_admin_without_permission(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Capacity" not in body
    assert 'href="/admin/capacity"' not in body


def test_admin_capacity_direct_access_denied_for_admin_without_permission(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/capacity", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/admin/dashboard")
    followed = client.get("/admin/capacity", follow_redirects=True)
    assert b"Access denied" in followed.data


def test_success_page_contact_support_uses_contact_route(client, login_user, app_module, db_session):
    project = app_module.Project(name="Navigation Success Project", owner_user_id=login_user.id)
    db_session.add(project)
    db_session.commit()

    response = client.get(f"/success/{project.id}")
    assert response.status_code == 200
    assert b"Contact Support" in response.data
    assert b'href="/contact"' in response.data
    assert b'href="#"' not in response.data
