from werkzeug.security import generate_password_hash


def _login_as(client, admin, session_role=None):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
        sess["admin_email"] = admin.email
        if session_role:
            sess["admin_role"] = session_role


def _assert_denied_to_dashboard(response):
    assert response.status_code == 302
    assert "/admin/dashboard" in response.headers["Location"]


def test_normal_admin_cannot_access_superadmin_platform_routes(client, app_module, db_session, secondary_admin, plan):
    _login_as(client, secondary_admin)
    original_active = plan.is_active

    for path in (
        "/admin/admins",
        "/admin/admins/add",
        "/admin/plans",
        "/admin/settings",
        "/admin/activity-logs",
        "/admin/subscriptions",
    ):
        _assert_denied_to_dashboard(client.get(path, follow_redirects=False))

    _assert_denied_to_dashboard(
        client.post(f"/admin/plans/{plan.id}/toggle-status", follow_redirects=False)
    )
    assert app_module.SubscriptionPlan.query.get(plan.id).is_active is original_active
    assert app_module.AdminActivity.query.filter_by(
        admin_id=secondary_admin.id,
        activity_type="access_denied",
    ).count() >= 7


def test_normal_admin_keeps_permitted_support_routes(client, secondary_admin, project_with_pair, normal_user):
    project, _pair = project_with_pair
    _login_as(client, secondary_admin)

    for path in (
        "/admin/dashboard",
        "/admin/users",
        f"/admin/users/{normal_user.id}",
        "/admin/user-profiles",
        "/admin/projects",
        f"/admin/projects/{project.id}",
        "/admin/payments",
        "/admin/scans",
        f"/admin/scans/user/{normal_user.id}",
    ):
        assert client.get(path).status_code == 200, path


def test_superadmin_can_access_platform_control_routes(client, admin):
    _login_as(client, admin)

    for path in (
        "/admin/admins",
        "/admin/admins/add",
        "/admin/plans",
        "/admin/settings",
        "/admin/activity-logs",
        "/admin/subscriptions",
    ):
        assert client.get(path).status_code == 200, path


def test_admin_authorization_uses_database_role_not_stale_session(client, app_module, db_session, admin, secondary_admin):
    secondary_admin.role = "superadmin"
    db_session.commit()
    _login_as(client, secondary_admin, session_role="superadmin")

    secondary_admin.role = "admin"
    db_session.commit()

    _assert_denied_to_dashboard(client.get("/admin/plans", follow_redirects=False))
    assert app_module.AdminActivity.query.filter_by(
        admin_id=secondary_admin.id,
        activity_type="access_denied",
    ).count() == 1


def test_inactive_or_malformed_admin_role_clears_session(client, app_module, db_session, secondary_admin):
    _login_as(client, secondary_admin)
    secondary_admin.role = "owner"
    db_session.commit()

    response = client.get("/admin/dashboard", follow_redirects=False)

    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "admin_id" not in sess


def test_login_rejects_malformed_admin_role(client, app_module, db_session):
    bad_admin = app_module.Admin(
        email="bad-role-admin@example.com",
        name="Bad Role",
        password_hash=generate_password_hash("AdminPass123"),
        role="owner",
        is_active=True,
    )
    db_session.add(bad_admin)
    db_session.commit()

    response = client.post(
        "/admin/login",
        data={"email": bad_admin.email, "password": "AdminPass123"},
    )

    assert response.status_code == 200
    assert b"Invalid email or password" in response.data
    with client.session_transaction() as sess:
        assert "admin_id" not in sess


def test_superadmin_create_and_edit_reject_unknown_roles(client, app_module, db_session, admin, secondary_admin):
    _login_as(client, admin)

    add_response = client.post(
        "/admin/admins/add",
        data={
            "email": "owner-role@example.com",
            "name": "Owner Role",
            "phone": "123",
            "role": "owner",
            "password": "AdminPass123",
        },
    )
    assert add_response.status_code == 200
    assert b"Invalid admin role" in add_response.data
    assert app_module.Admin.query.filter_by(email="owner-role@example.com").first() is None

    edit_response = client.post(
        f"/admin/admins/{secondary_admin.id}/edit",
        data={
            "name": secondary_admin.name,
            "phone": secondary_admin.phone or "",
            "role": "owner",
            "is_active": "on",
        },
    )
    assert edit_response.status_code == 200
    assert b"Invalid admin role" in edit_response.data
    assert app_module.Admin.query.get(secondary_admin.id).role == "admin"


def test_final_active_superadmin_cannot_be_demoted_or_deactivated(client, app_module, db_session, admin):
    _login_as(client, admin)

    demote = client.post(
        f"/admin/admins/{admin.id}/edit",
        data={"name": admin.name, "phone": admin.phone or "", "role": "admin", "is_active": "on"},
    )
    assert demote.status_code == 200
    assert b"active super admin" in demote.data
    assert app_module.Admin.query.get(admin.id).role == "superadmin"

    toggle = client.post(f"/admin/admins/{admin.id}/toggle-status", follow_redirects=True)
    assert toggle.status_code == 200
    assert b"deactivate your own account" in toggle.data
    assert app_module.Admin.query.get(admin.id).is_active is True


def test_one_of_two_superadmins_can_be_demoted(client, app_module, db_session, admin, secondary_admin):
    secondary_admin.role = "superadmin"
    db_session.commit()
    _login_as(client, admin)

    response = client.post(
        f"/admin/admins/{secondary_admin.id}/edit",
        data={
            "name": secondary_admin.name,
            "phone": secondary_admin.phone or "",
            "role": "admin",
            "is_active": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    refreshed = app_module.Admin.query.get(secondary_admin.id)
    assert refreshed.role == "admin"
    assert refreshed.is_active is True
    assert app_module.AdminActivity.query.filter_by(activity_type="admin_role_change").count() == 1


def test_normal_admin_navigation_hides_superadmin_links(client, secondary_admin):
    _login_as(client, secondary_admin)

    body = client.get("/admin/dashboard").get_data(as_text=True)

    assert "/admin/admins" not in body
    assert "/admin/plans" not in body
    assert "/admin/settings" not in body
