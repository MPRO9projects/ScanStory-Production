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
