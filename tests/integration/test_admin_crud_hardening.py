def test_superadmin_can_create_admin(client, app_module, login_admin):
    response = client.post(
        "/admin/admins/add",
        data={"email": "new-admin@example.com", "name": "New Admin", "phone": "123", "role": "admin", "password": "AdminPass123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert app_module.Admin.query.filter_by(email="new-admin@example.com").first() is not None


def test_duplicate_admin_is_rejected(client, login_admin, secondary_admin):
    response = client.post(
        "/admin/admins/add",
        data={"email": secondary_admin.email, "name": "Duplicate", "role": "admin", "password": "AdminPass123"},
    )
    assert response.status_code == 200
    assert b"already exists" in response.data


def test_superadmin_can_edit_admin(client, app_module, login_admin, secondary_admin):
    response = client.post(
        f"/admin/admins/{secondary_admin.id}/edit",
        data={"name": "Edited Admin", "phone": "999", "role": "admin", "is_active": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    refreshed = app_module.Admin.query.get(secondary_admin.id)
    assert refreshed.name == "Edited Admin"
    assert refreshed.is_active is True


def test_superadmin_can_disable_admin(client, app_module, login_admin, secondary_admin):
    response = client.post(
        f"/admin/admins/{secondary_admin.id}/edit",
        data={"name": secondary_admin.name, "phone": secondary_admin.phone or "", "role": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert app_module.Admin.query.get(secondary_admin.id).is_active is False


def test_admin_plan_creation_requires_pair_limit(client, login_admin):
    response = client.post("/admin/plans/add", data={"plan_name": "No Pair Limit"})
    assert response.status_code == 200
    assert b"Pairs allowed per project is required" in response.data


def test_admin_can_create_plan(client, app_module, login_admin):
    response = client.post(
        "/admin/plans/add",
        data={
            "plan_name": "Gate B Plan",
            "plan_amount": "10",
            "currency": "INR",
            "duration_type": "time",
            "duration_value": "1",
            "total_project_limit": "2",
            "total_scan_limit": "20",
            "max_pairs_per_project": "3",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert app_module.SubscriptionPlan.query.filter_by(plan_name="Gate B Plan").first() is not None
