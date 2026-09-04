import importlib
import re
import sys

from werkzeug.security import generate_password_hash

# Track B (2026-08-31): this used to import _extract_hidden_csrf_token/
# _reimport_app_with_real_csrf from tests.security.test_csrf_and_headers via
# an absolute `tests.xxx` import - broken pytest COLLECTION (not a test
# failure) because a stray global site-packages `tests` package shadows
# dotted `tests.xxx` imports in this environment, confirmed pre-existing
# (other files in this suite already document and work around the exact
# same issue - e.g. test_issue3e_c_multi_video_upload.py's own docstring).
# Duplicated locally, matching that established convention, rather than
# fighting the import shadow.
def _reimport_app_with_real_csrf(monkeypatch, tmp_path, extra_env=None):
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'csrf.db').as_posix()}")
    monkeypatch.setenv("SCANSTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCANSTORY_ADMIN_DATA_DIR", str(tmp_path / "data_admin"))
    monkeypatch.setenv("SCANSTORY_STATIC_UPLOADS_DIR", str(tmp_path / "static_uploads"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "csrf-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_ENABLED", raising=False)
    for key, value in (extra_env or {}).items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    app_module = importlib.import_module("app")
    # Deliberately NOT forcing WTF_CSRF_ENABLED=False here - real enforcement is the point.
    app_module.app.config.update(TESTING=True)
    return app_module


def _extract_hidden_csrf_token(html_text):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html_text)
    assert match, "no csrf_token hidden input found in response HTML"
    return match.group(1)


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
