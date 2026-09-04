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
3. Mobile drawer nav unreachable: admin-console.css's off-canvas drawer rule
   (id selector, specificity beats admin/base.html's class-only rule
   regardless of source order) set the open drawer's z-index to 1002, below
   the backdrop's 1029. The backdrop painted over every open link, so any tap
   just closed the drawer via the backdrop's click handler - admins and
   superadmins could never leave Dashboard on mobile. Also tightened the
   768px/992px breakpoints to Bootstrap's exact 767.98/768/991.98 so no
   viewport width fell into a dead zone matched by neither the tablet-rail
   nor the off-canvas-drawer media query.
"""
from pathlib import Path

import pytest


ADMIN_TEMPLATES = Path("templates/admin")

AUTHENTICATED_ADMIN_SHELL_TEMPLATES = (
    "activity_logs.html",
    "add_admin.html",
    "add_plan.html",
    "addons.html",
    "capacity.html",
    "dashboard.html",
    "edit_admin.html",
    "edit_plan.html",
    "manage_admins.html",
    "moderation.html",
    "operations.html",
    "ownership.html",
    "payments.html",
    "plans.html",
    "projects.html",
    "scans.html",
    "settings.html",
    "subscriptions.html",
    "user_dashboard_context.html",
    "user_scans.html",
    "users.html",
    "view_payment.html",
    "view_project.html",
    "view_user.html",
    "webhook_events.html",
)

INTENTIONAL_STANDALONE_ADMIN_TEMPLATES = (
    "base.html",
    "forgot_password.html",
    "login.html",
    "project_preview.html",
    "reset_password.html",
    "reset_password_email.html",
)


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

# ---------------------------------------------------------------------------
# World-Class Admin Restructure (2026-09-02) superseded the single canonical
# "/admin/projects" nav entry with three ownership-scoped destinations (My
# Projects / All Admin Projects [Super Admin] / Customer Projects) - the
# whole point of that pass was reversing the "one link, ambiguous meaning"
# decision this file used to guard. Updated in place rather than deleted so
# the surrounding still-valid coverage (dashboard redirects, dropdown JS,
# mobile drawer, capacity permission) stays intact.
# ---------------------------------------------------------------------------

def test_admin_nav_splits_projects_into_ownership_scoped_entries(client, login_admin):
    response = client.get("/admin/dashboard")
    data = response.data
    assert b'href="/admin/my-projects"' in data
    assert b'href="/admin/customer-projects"' in data
    # login_admin is a superadmin fixture - All Admin Projects must be visible.
    assert b'href="/admin/admin-projects"' in data
    assert b"My Projects" in data


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
    # Sidebar label renamed Users -> Customers (restructure §15); the backend
    # route/endpoint name (admin_users) is unchanged.
    assert data.count(b'<span>Customers</span>') == 1
    assert b"User Profiles" not in data
    assert b'href="/admin/user-profiles"' not in data


def test_my_projects_page_shows_correct_active_nav_state(client, login_admin):
    response = client.get("/admin/my-projects")
    assert response.status_code == 200
    assert b'href="/admin/my-projects" class="sidebar-link active"' in response.data


def test_project_detail_page_shows_correct_active_nav_state(client, login_admin, app_module, db_session):
    project = app_module.Project(name="Admin Owned Project", owner_admin_id=login_admin.id)
    db_session.add(project)
    db_session.commit()
    response = client.get(f"/admin/projects/{project.id}")
    assert response.status_code == 200
    assert b'href="/admin/customer-projects" class="sidebar-link active"' in response.data


# ---------------------------------------------------------------------------
# 10: /admin/my-projects is now a real page, not a redirect shim
# ---------------------------------------------------------------------------

def test_my_projects_route_is_a_real_page_not_a_redirect(client, login_admin, app_module, db_session):
    project = app_module.Project(name="Own Workspace Project", owner_admin_id=login_admin.id)
    db_session.add(project)
    db_session.commit()
    response = client.get("/admin/my-projects", follow_redirects=False)
    assert response.status_code == 200
    assert b"Own Workspace Project" in response.data


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
    assert 'href="/admin/capacity" class="sidebar-link active"' in body
    assert 'aria-current="page"' in body


def test_admin_base_does_not_load_unused_chart_library_globally():
    base = open("templates/admin/base.html", encoding="utf-8").read()
    assert "cdn.jsdelivr.net/npm/chart.js" not in base
    assert "Chart.js" not in base


def test_admin_nav_hides_capacity_link_for_admin_without_permission(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Capacity" not in body
    assert 'href="/admin/capacity"' not in body


def test_authenticated_admin_templates_extend_shared_shell():
    for name in AUTHENTICATED_ADMIN_SHELL_TEMPLATES:
        html = (ADMIN_TEMPLATES / name).read_text(encoding="utf-8")
        assert html.startswith('{% extends "admin/base.html" %}')
        assert "<!DOCTYPE" not in html
        assert '{% include "admin/_sidebar_links.html" %}' not in html


def test_only_auth_email_and_preview_admin_templates_are_standalone():
    standalone = {
        path.name
        for path in ADMIN_TEMPLATES.glob("*.html")
        if "<!DOCTYPE" in path.read_text(encoding="utf-8") or "<html" in path.read_text(encoding="utf-8")
    }
    assert standalone == set(INTENTIONAL_STANDALONE_ADMIN_TEMPLATES)


def test_retired_admin_templates_are_deleted():
    assert not (ADMIN_TEMPLATES / "my_projects.html").exists()
    assert not (ADMIN_TEMPLATES / "user_profiles.html").exists()


def test_sidebar_active_state_contains_no_redirect_only_endpoints():
    html = (ADMIN_TEMPLATES / "_sidebar_links.html").read_text(encoding="utf-8")
    assert "admin_user_profiles" not in html
    # admin_my_projects is intentionally in the sidebar now - it's a real
    # page (World-Class Admin Restructure), not the old redirect shim.


def test_admin_addons_shared_shell_keeps_critical_forms(client, login_admin):
    response = client.get("/admin/addons")
    assert response.status_code == 200
    body = response.data
    assert b'id="adminSidebar"' in body
    assert b"/admin/addons/create" in body
    assert b'name="addon_type"' in body
    assert b'name="unit_amount"' in body
    assert b"Items are never deleted, only deactivated" in body


def test_migrated_admin_workspace_pages_keep_critical_controls(client, login_admin):
    checks = {
        "/admin/users": (b'id="adminSidebar"', b"User Management", b"name=\"search\""),
        "/admin/projects": (b'id="adminSidebar"', b"Projects", b"name=\"owner_type\""),
        "/admin/payments": (b'id="adminSidebar"', b"Payments", b"name=\"method\""),
        "/admin/scans": (b'id="adminSidebar"', b"Scans", b"Scan"),
        "/admin/plans": (b'id="adminSidebar"', b"Plan Management", b"Add New Plan"),
        "/admin/admins": (b'id="adminSidebar"', b"Admin Management", b"Add New Admin"),
        "/admin/settings": (b'id="adminSidebar"', b"Admin Settings", b"Trial Settings"),
    }
    for path, expected in checks.items():
        response = client.get(path)
        assert response.status_code == 200
        body = response.data
        for marker in expected:
            assert marker in body


def test_admin_console_css_hardens_tables_forms_and_modals():
    css = Path("static/css/admin-console.css").read_text(encoding="utf-8")
    assert "overflow-wrap: anywhere" in css
    assert ".ss-admin-scope .table-container" in css
    assert ".ss-admin-scope .modal.show" in css
    assert ".ss-admin-scope .action-buttons .btn" in css


def test_admin_console_css_mobile_drawer_outranks_backdrop():
    """Regression guard for the "stuck on Dashboard" mobile nav bug: the
    #adminSidebar id selector always beats base.html's class-only .sidebar
    rule, so this file's own drawer z-index (not base.html's) is what
    actually has to clear the backdrop's 1029."""
    css = Path("static/css/admin-console.css").read_text(encoding="utf-8")

    drawer_block_start = css.index("#adminSidebar.sidebar {")
    drawer_block_end = css.index("}", drawer_block_start)
    drawer_block = css[drawer_block_start:drawer_block_end]
    assert "z-index: 1030" in drawer_block

    base_css = Path("templates/admin/base.html").read_text(encoding="utf-8")
    backdrop_block_start = base_css.index(".sidebar-backdrop {")
    backdrop_block_end = base_css.index("}", backdrop_block_start)
    assert "z-index: 1029" in base_css[backdrop_block_start:backdrop_block_end]

    # No dead zone: the tablet-rail and off-canvas-drawer breakpoints must
    # meet exactly at Bootstrap's md boundary (767.98 / 768), not overlap
    # or leave a gap where a viewport matches neither.
    assert "max-width: 991.98px) and (min-width: 768px)" in css
    assert "@media (max-width: 767.98px)" in css
    assert "@media (max-width: 768px)" not in css


def test_admin_capacity_direct_access_denied_for_admin_without_permission(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/capacity", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/admin/dashboard")
    followed = client.get("/admin/capacity", follow_redirects=True)
    assert b"Access denied" in followed.data


def test_superadmin_can_open_operations_diagnostics(client, login_admin, app_module, db_session, normal_user):
    upload = app_module.UploadSession(
        owner_user_id=normal_user.id,
        purpose="project_pair",
        project_name="Diagnostics Upload",
        original_image_name="C:/secret/path/marker.jpg",
        original_video_name="/private/video.mp4",
        image_size=10,
        video_size=20,
        expected_total_size=30,
        current_offset=10,
        status="active",
        storage_token="11111111-1111-4111-8111-111111111111",
        expires_at=app_module.get_utc_now() + app_module.timedelta(minutes=10),
    )
    job = app_module.ProcessingJob(
        public_key="job_ops_diag",
        job_type="process_project_pairs",
        status="failed",
        idempotency_key="ops-diag",
        safe_error_code="QUEUE_UNAVAILABLE",
        safe_error_summary="Processing queue is unavailable.",
    )
    db_session.add_all([upload, job])
    db_session.commit()

    response = client.get("/admin/operations")
    assert response.status_code == 200
    body = response.data
    assert b"Operations Diagnostics" in body
    assert b"Recent Upload Sessions" in body
    assert b"Recent Processing Jobs" in body
    assert b"Current Entitlement Visibility" in body
    assert b"Recent Entitlement Ledger" in body
    assert b"marker.jpg" in body
    assert b"video.mp4" in body
    assert b"C:/secret/path" not in body
    assert b"/private/" not in body
    assert b"SMTP_PASS" not in body
    # Refund/add-on-refund actions relocated to Payments > Refunds
    # (World-Class Admin Restructure) - Operations keeps only a summary + link.
    assert b"Recent Add-on Purchases" not in body
    assert b'href="/admin/payments/refunds"' in body

    refunds_response = client.get("/admin/payments/refunds")
    assert refunds_response.status_code == 200
    assert b"Recent Add-on Purchases" in refunds_response.data


def test_normal_admin_cannot_open_operations_diagnostics(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    response = client.get("/admin/operations", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/admin/dashboard")


# ---------------------------------------------------------------------------
# Issue 1B: Profile dropdown inert / Logout unreachable on every migrated
# admin page except Dashboard. Root cause: 13 templates carried leftover
# pre-Bootstrap-migration dropdown-toggle JS in their own {% block extra_js
# %} - a window.onclick handler whose guard (`!event.target.matches(
# '.user-menu span')`) referenced a selector no longer present anywhere in
# the current markup, so it was always true and unconditionally closed any
# open .dropdown-menu on *every* click, including the same click Bootstrap's
# native data-bs-toggle="dropdown" (in admin/base.html) had just used to
# open it. Dashboard/Operations/Subscriptions never carried this dead code,
# which is why only they worked. Fix: delete the dead handler + the
# toggleDropdown() function it paired with from all 13 templates and rely
# solely on base.html's shared, already-correct Bootstrap dropdown.
# ---------------------------------------------------------------------------

DROPDOWN_DEAD_CODE_TEMPLATES = (
    "add_plan.html",
    "edit_plan.html",
    "manage_admins.html",
    "payments.html",
    "plans.html",
    "projects.html",
    "scans.html",
    "settings.html",
    "user_scans.html",
    "users.html",
    "view_payment.html",
    "view_project.html",
    "view_user.html",
)


def test_admin_pages_no_longer_carry_dead_dropdown_toggle_js():
    for name in DROPDOWN_DEAD_CODE_TEMPLATES:
        html = (ADMIN_TEMPLATES / name).read_text(encoding="utf-8")
        assert "window.onclick" not in html, name
        assert "toggleDropdown" not in html, name
        assert ".user-menu span" not in html, name


def test_admin_base_shell_is_the_only_place_defining_the_profile_dropdown():
    for path in ADMIN_TEMPLATES.glob("*.html"):
        if path.name == "base.html":
            continue
        html = path.read_text(encoding="utf-8")
        assert 'id="userDropdown"' not in html, path.name
        assert "data-bs-toggle=\"dropdown\"" not in html, path.name


@pytest.mark.parametrize("path", [
    "/admin/dashboard",
    "/admin/users",
    "/admin/projects",
    "/admin/payments",
    "/admin/plans",
    "/admin/admins",
    "/admin/scans",
    "/admin/settings",
    "/admin/operations",
    "/admin/subscriptions",
])
def test_admin_profile_dropdown_and_logout_render_intact_on_every_page(client, login_admin, path):
    response = client.get(path)
    assert response.status_code == 200
    body = response.data.decode()
    assert 'id="userDropdown"' in body
    assert 'data-bs-toggle="dropdown"' in body
    assert body.count('id="userDropdown"') == 1
    assert "window.onclick" not in body
    assert "toggleDropdown" not in body
    assert '<a class="dropdown-item" href="/admin/logout"' in body


def test_admin_profile_dropdown_and_logout_render_intact_for_non_superadmin(client, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = secondary_admin.id
    for path in ("/admin/dashboard", "/admin/users", "/admin/projects"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.data.decode()
        assert 'id="userDropdown"' in body
        assert 'data-bs-toggle="dropdown"' in body
        assert "window.onclick" not in body
        assert '<a class="dropdown-item" href="/admin/logout"' in body


def test_admin_logout_route_still_works_after_dead_js_removal(client, login_admin):
    response = client.get("/admin/logout", follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert "admin_id" not in sess


def test_success_page_contact_support_uses_contact_route(client, login_user, app_module, db_session):
    project = app_module.Project(name="Navigation Success Project", owner_user_id=login_user.id)
    db_session.add(project)
    db_session.commit()

    response = client.get(f"/success/{project.id}")
    assert response.status_code == 200
    assert b"contact support" in response.data.lower()
    assert b'href="/contact"' in response.data
    assert b'href="#"' not in response.data
