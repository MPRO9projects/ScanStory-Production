from werkzeug.security import generate_password_hash

from tests.security.test_csrf_and_headers import _extract_hidden_csrf_token, _reimport_app_with_real_csrf


def test_admin_project_suspend_requires_csrf_token(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path)
    client = app_module.app.test_client()

    with app_module.app.app_context():
        admin = app_module.Admin(
            email="csrf-admin@example.com",
            name="CSRF Admin",
            password_hash=generate_password_hash("AdminPass123"),
            role="superadmin",
            is_active=True,
        )
        app_module.db.session.add(admin)
        app_module.db.session.commit()
        project = app_module.Project(name="CSRF Project", owner_admin_id=admin.id)
        app_module.db.session.add(project)
        app_module.db.session.commit()
        project_id = project.id
        admin_id = admin.id

    with client.session_transaction() as sess:
        sess["admin_id"] = admin_id

    rejected = client.post(f"/admin/projects/{project_id}/suspend")
    assert rejected.status_code == 400

    page = client.get(f"/admin/projects/{project_id}")
    token = _extract_hidden_csrf_token(page.get_data(as_text=True))
    accepted = client.post(f"/admin/projects/{project_id}/suspend", data={"csrf_token": token})
    assert accepted.status_code == 302

    with app_module.app.app_context():
        assert app_module.Project.query.get(project_id).is_active is False
