"""V1.1 P0-3 (PostgreSQL URL/driver) plus the two adjacent gaps.

Adjacent: `requests` declared as a direct dependency, and the admin delete
form's CSRF token.
"""
import pytest

from core.config import normalize_database_url


@pytest.mark.parametrize("raw", ["postgresql://u:p@h:5432/db", "postgres://u:p@h:5432/db"])
def test_bare_postgres_urls_are_pinned_to_psycopg_v3(raw):
    assert normalize_database_url(raw) == "postgresql+psycopg://u:p@h:5432/db"


def test_explicit_psycopg_url_is_unchanged():
    raw = "postgresql+psycopg://u:p@h:5432/db?sslmode=require"
    assert normalize_database_url(raw) == raw


@pytest.mark.parametrize("driver", ["psycopg2", "asyncpg", "pg8000"])
def test_unsupported_postgres_drivers_are_rejected_with_a_named_reason(driver):
    with pytest.raises(RuntimeError) as excinfo:
        normalize_database_url(f"postgresql+{driver}://user:secretpw@h/db")
    message = str(excinfo.value)
    assert driver in message
    assert "psycopg" in message
    assert "secretpw" not in message, "error text must never carry credentials"
    assert "@h/db" not in message


def test_credentials_and_query_parameters_survive_normalization():
    from sqlalchemy.engine import make_url

    raw = "postgresql://us%40er:p%40ss%2Fword@db.example.com:6543/appdb?sslmode=require&connect_timeout=5"
    url = make_url(normalize_database_url(raw))

    assert url.get_driver_name() == "psycopg"
    assert url.username == "us@er"
    assert url.password == "p@ss/word"
    assert url.host == "db.example.com"
    assert url.port == 6543
    assert url.database == "appdb"
    assert dict(url.query) == {"sslmode": "require", "connect_timeout": "5"}


@pytest.mark.parametrize("raw", [
    "sqlite:///relative.db",
    "sqlite:////abs/path.db",
    "sqlite:///:memory:",
    "mysql+pymysql://u:p@h/db",
    "",
    None,
    "not-a-url",
])
def test_non_postgres_urls_are_untouched(raw):
    assert normalize_database_url(raw) == raw


def test_running_test_app_still_uses_sqlite(app_module):
    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")


def test_normalized_postgres_url_passes_the_backend_gate():
    from core.config import database_backend_name

    assert database_backend_name(normalize_database_url("postgresql://u:p@h/db")) == "postgresql"


# ===========================================================================
# ADJACENT FIXES
# ===========================================================================
def test_requests_is_declared_as_a_direct_dependency():
    """app.py imports requests directly; it must not ride in transitively."""
    from pathlib import Path

    requirements = Path(__file__).resolve().parents[2] / "requirements.txt"
    declared = [
        line.split("#", 1)[0].strip().lower()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(entry.startswith("requests") for entry in declared)
    # psycopg v3 remains the only PostgreSQL driver declared.
    assert any(entry.startswith("psycopg[") or entry.startswith("psycopg<") for entry in declared)
    assert not any(entry.startswith("psycopg2") for entry in declared)


def test_admin_delete_form_emits_a_csrf_token(app_module, client, admin, secondary_admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    response = client.get(f"/admin/admins/{secondary_admin.id}/edit")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    delete_action = f"/admin/admins/{secondary_admin.id}/delete"
    assert delete_action in body
    delete_form = body[body.index(delete_action):]
    delete_form = delete_form[:delete_form.index("</form>")]
    assert 'name="csrf_token"' in delete_form, "admin delete form must carry a CSRF token"


def test_admin_delete_rejects_a_request_without_a_csrf_token(app_module, admin, secondary_admin):
    """CSRF enforcement itself is still on for that endpoint."""
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        csrf_client = app_module.app.test_client()
        with csrf_client.session_transaction() as sess:
            sess["admin_id"] = admin.id
        response = csrf_client.post(f"/admin/admins/{secondary_admin.id}/delete")
        assert response.status_code in (400, 403)
        assert app_module.Admin.query.get(secondary_admin.id) is not None
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = False
