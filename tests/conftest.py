import importlib
import os
import smtplib
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from werkzeug.security import generate_password_hash


def _assert_under(child, parent):
    child_path = Path(child).resolve()
    parent_path = Path(parent).resolve()
    assert child_path == parent_path or parent_path in child_path.parents


@pytest.fixture()
def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    admin_data_dir = tmp_path / "data_admin"
    static_uploads_dir = tmp_path / "static_uploads"

    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SCANSTORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SCANSTORY_ADMIN_DATA_DIR", str(admin_data_dir))
    monkeypatch.setenv("SCANSTORY_STATIC_UPLOADS_DIR", str(static_uploads_dir))
    monkeypatch.setenv("FLASK_SECRET_KEY", "gate-a-test-secret")
    # Tests explicitly opt in to bootstrap admin creation (same identity the
    # suite has always relied on) rather than getting it via a hard-coded
    # production default.
    monkeypatch.setenv("BOOTSTRAP_ADMIN_ENABLED", "1")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@scanstory.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "gate-a-test-admin-pw")
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)

    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    assert os.environ.get("SCANSTORY_TESTING") == "1"
    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")
    _assert_under(app_module.DATA_DIR, tmp_path)
    _assert_under(app_module.ADMIN_DATA_DIR, tmp_path)
    _assert_under(app_module.STATIC_UPLOADS_DIR, tmp_path)
    for path in [
        app_module.IMAGES_DIR, app_module.VIDEOS_DIR, app_module.FEATURES_DIR, app_module.QR_DIR,
        app_module.ADMIN_IMAGES_DIR, app_module.ADMIN_VIDEOS_DIR, app_module.ADMIN_FEATURES_DIR,
        app_module.ADMIN_QR_DIR,
    ]:
        _assert_under(path, tmp_path)

    sent_emails = []
    blocked_external_calls = []

    def fake_send_email(to_email, subject, html_body):
        sent_emails.append({"to": to_email, "subject": subject, "html": html_body})
        return True

    def blocked_smtp(*args, **kwargs):
        blocked_external_calls.append({"service": "smtp", "args": args})
        raise AssertionError("Unmocked SMTP call blocked in tests")

    def blocked_requests(self, method, url, *args, **kwargs):
        if str(url).startswith(("http://localhost", "http://127.0.0.1")):
            raise AssertionError("Backend external HTTP call attempted in unit test")
        blocked_external_calls.append({"service": "http", "method": method, "url": url})
        raise AssertionError(f"Unmocked external HTTP call blocked: {method} {url}")

    monkeypatch.setattr(app_module, "send_email_smtp", fake_send_email)
    monkeypatch.setattr(app_module, "verify_recaptcha_v3", lambda action: (True, "OK"))
    monkeypatch.setattr(smtplib, "SMTP", blocked_smtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", blocked_smtp)
    monkeypatch.setattr(app_module.requests.sessions.Session, "request", blocked_requests)
    app_module._gate_b_blocked_external_calls = blocked_external_calls

    ctx = app_module.app.app_context()
    ctx.push()

    yield app_module, sent_emails, tmp_path

    engine = app_module.db.engine
    try:
        app_module.db.session.remove()
        app_module.db.drop_all()
    finally:
        ctx.pop()
    engine.dispose()


@pytest.fixture()
def app_module(isolated_app):
    return isolated_app[0]


@pytest.fixture()
def app(app_module):
    return app_module.app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_session(app_module):
    with app_module.app.app_context():
        yield app_module.db.session


@pytest.fixture()
def plan(app_module, db_session):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()


@pytest.fixture()
def normal_user(app_module, db_session, plan):
    user = app_module.User(
        email="user@example.com",
        first_name="Normal",
        last_name="User",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscription_taken_at=datetime.utcnow(),
        subscribed_project_limit=plan.total_project_limit,
        subscribed_scan_limit=plan.total_scan_limit,
        projects_used=0,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    trial = app_module.TrialDetails(
        user_id=user.id,
        trial_start=datetime.utcnow(),
        trial_end=datetime.utcnow() + timedelta(days=7),
        trial_project_limit=plan.total_project_limit,
        trial_scan_limit=plan.total_scan_limit,
    )
    db_session.add(trial)
    db_session.commit()
    return user


@pytest.fixture()
def expired_user(app_module, db_session, plan):
    user = app_module.User(
        email="expired@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
        subscription_taken_at=datetime.utcnow() - timedelta(days=10),
        subscribed_project_limit=1,
        subscribed_scan_limit=1,
    )
    db_session.add(user)
    db_session.commit()
    trial = app_module.TrialDetails(
        user_id=user.id,
        trial_start=datetime.utcnow() - timedelta(days=10),
        trial_end=datetime.utcnow() - timedelta(days=1),
        trial_project_limit=1,
        trial_scan_limit=1,
    )
    db_session.add(trial)
    db_session.commit()
    return user


@pytest.fixture()
def admin(app_module):
    return app_module.Admin.query.filter_by(email="admin@scanstory.com").first()


@pytest.fixture()
def admin_password():
    """The bootstrap admin's real password, from the same isolated test
    configuration (isolated_app) that created it - never a hard-coded
    literal, and never the removed "Admin@123" default."""
    return os.environ["BOOTSTRAP_ADMIN_PASSWORD"]


@pytest.fixture()
def secondary_admin(app_module, db_session, admin):
    secondary = app_module.Admin(
        email="secondary-admin@example.com",
        name="Secondary Admin",
        password_hash=generate_password_hash("AdminPass123"),
        role="admin",
        is_active=True,
        created_by=admin.id,
    )
    db_session.add(secondary)
    db_session.commit()
    return secondary


@pytest.fixture()
def project_with_pair(app_module, db_session, normal_user, tmp_path):
    project = app_module.Project(
        name="Baseline Project",
        owner_user_id=normal_user.id,
        user_project_index=1,
        scanner_url="/scanner/1",
        qr_code_filename="project_1_main.png",
        qr_code_path="/qr/project_1_main.png",
    )
    db_session.add(project)
    db_session.commit()

    image_path = Path(app_module.IMAGES_DIR) / f"{project.id}_0.jpg"
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"
    qr_path = Path(app_module.QR_DIR) / "project_1_main.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake image")
    video_path.write_bytes(b"fake video")
    qr_path.write_bytes(b"fake qr")

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=image_path.name,
        video_filename=video_path.name,
        image_path=f"/image/{project.id}/0",
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()
    project.scanner_url = f"/scanner/{project.id}"
    db_session.commit()
    return project, pair


@pytest.fixture()
def multiple_pairs(app_module, db_session, project_with_pair):
    project, first_pair = project_with_pair
    for index in (1, 2):
        pair = app_module.ProjectPair(
            project_id=project.id,
            pair_index=index,
            image_filename=f"{project.id}_{index}.jpg",
            video_filename=f"{project.id}_{index}.mp4",
            image_path=f"/image/{project.id}/{index}",
            is_processed=True,
            processing_status="completed",
            feature_extraction_status="extracted",
        )
        db_session.add(pair)
    db_session.commit()
    return project


@pytest.fixture()
def feature_artifact(app_module, project_with_pair):
    project, pair = project_with_pair
    path = Path(app_module.FEATURES_DIR) / f"{project.id}_{pair.pair_index}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"w": np.int32(100), "h": np.int32(100)}
    for tag in app_module.FEATURE_TAGS:
        payload[f"desc_{tag}"] = np.zeros((0, 32), dtype=np.uint8)
        payload[f"kp_{tag}"] = np.zeros((0, 2), dtype=np.float32)
    np.savez(path, **payload)
    app_module.load_features.cache_clear()
    return path


@pytest.fixture()
def login_user(client, normal_user):
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    return normal_user


@pytest.fixture()
def login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    return admin


@pytest.fixture()
def invalid_binary_file():
    return b"not a real image or video"


@pytest.fixture()
def path_traversal_filename():
    return "..\\..\\evil.jpg"
