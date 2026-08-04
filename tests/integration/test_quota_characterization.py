from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


class NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        return None


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _jpeg_bytes(width=640, height=480):
    out = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(out, format="JPEG")
    out.seek(0)
    return out


def _upload_data(name="Quota Project", pair_count=1):
    data = {"name": name, "upload_id": f"quota-{name}"}
    data["images"] = []
    data["videos"] = []
    for index in range(pair_count):
        data["images"].append((_jpeg_bytes(), f"marker-{index}.jpg"))
        data["videos"].append((BytesIO(b"video"), f"clip-{index}.mp4"))
        data[f"marker_{index}_mode"] = "crop"
        data[f"marker_{index}_crop_x"] = "0.1"
        data[f"marker_{index}_crop_y"] = "0.1"
        data[f"marker_{index}_crop_width"] = "0.6"
        data[f"marker_{index}_crop_height"] = "0.6"
        data[f"marker_{index}_rotation"] = "0"
        data[f"marker_{index}_original_width"] = "640"
        data[f"marker_{index}_original_height"] = "480"
        data[f"marker_{index}_processed_width"] = "520"
        data[f"marker_{index}_processed_height"] = "420"
        data[f"marker_{index}_source_size_bytes"] = "100000"
        data[f"marker_{index}_processed_size_bytes"] = "90000"
        data[f"marker_{index}_display_orientation"] = "landscape"
    return data


def _patch_upload_side_effects(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: Path(args[3]).write_bytes(b"qr"))
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _make_user_project(app_module, db_session, user, name="Quota Existing"):
    project = app_module.Project(name=name, owner_user_id=user.id)
    db_session.add(project)
    db_session.commit()
    return project


def _make_admin_project(app_module, db_session, admin, name="Admin Quota Existing"):
    project = app_module.Project(name=name, owner_admin_id=admin.id)
    db_session.add(project)
    db_session.commit()
    return project


def _make_pair(app_module, db_session, project, index=0, ready=True):
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=index,
        image_filename=f"{project.id}_{index}.jpg",
        video_filename=f"{project.id}_{index}.mp4",
        image_path=f"/image/{project.id}/{index}",
        is_processed=ready,
        processing_status="completed" if ready else "uploaded",
        feature_extraction_status="extracted" if ready else "pending",
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def _make_successful_scan(app_module, db_session, project, pair, user, session_id="quota-session"):
    scan = app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id if pair else None,
        user_id=user.id,
        scan_session_id=session_id,
        is_successful=True,
        counted=False,
    )
    db_session.add(scan)
    db_session.commit()
    return scan


def test_user_below_project_limit_can_create(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 2
    normal_user.projects_used = 1
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert app_module.Project.query.filter_by(owner_user_id=normal_user.id).count() == 1
    assert app_module.User.query.get(normal_user.id).projects_used == 2


def test_user_at_project_limit_is_blocked(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 1
    normal_user.projects_used = 1
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert "/subscribe" in response.headers["Location"]
    assert app_module.Project.query.filter_by(owner_user_id=normal_user.id).count() == 0
    assert app_module.User.query.get(normal_user.id).projects_used == 1


def test_user_cannot_bypass_project_limit_through_create_page(client, app_module, db_session, normal_user):
    normal_user.subscribed_project_limit = 1
    normal_user.projects_used = 1
    db_session.commit()
    _login_user(client, normal_user)

    response = client.get("/create-project", follow_redirects=False)

    assert response.status_code == 302
    assert "/subscribe" in response.headers["Location"]


def test_pair_below_limit_can_be_added_during_project_upload(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 2)
    normal_user.subscribed_project_limit = 2
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data=_upload_data(pair_count=2), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 2


def test_pair_at_limit_is_blocked_during_project_upload(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 1)
    normal_user.subscribed_project_limit = 2
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data=_upload_data(pair_count=2), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/create-project")
    assert app_module.Project.query.filter_by(owner_user_id=normal_user.id).count() == 0
    assert app_module.ProjectPair.query.count() == 0
    assert app_module.User.query.get(normal_user.id).projects_used == 0


def test_failed_upload_validation_does_not_consume_project_or_pair(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 2
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data={"name": "bad"}, content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/create-project")
    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0
    assert app_module.User.query.get(normal_user.id).projects_used == 0


def test_successful_scanner_session_consumes_one_scan(client, app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="scan-once")

    response = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "scan-once"})

    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["counted"] is True
    assert app_module.User.query.get(normal_user.id).scans_used == 1
    assert app_module.ScanLog.query.filter_by(scan_session_id="scan-once").one().counted is True


def test_repeated_session_end_deduplicates_scan_count(client, app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="repeat-session")

    first = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "repeat-session"}).get_json()
    second = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "repeat-session"}).get_json()

    assert first["counted"] is True
    assert second["counted"] is False
    assert second["reason"] == "Already counted"
    assert app_module.User.query.get(normal_user.id).scans_used == 1


def test_user_at_scan_limit_is_blocked_before_detection_processing(client, app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    _make_pair(app_module, db_session, project, ready=True)
    normal_user.subscribed_scan_limit = 1
    normal_user.scans_used = 1
    db_session.commit()

    response = client.post(
        "/detect_init",
        data={"project_id": project.id, "scan_session_id": "blocked-scan", "test_image": (_jpeg_bytes(), "frame.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 403
    assert response.get_json()["reason"] == "Scan limit reached. Please upgrade your plan."
    assert app_module.User.query.get(normal_user.id).scans_used == 1


def test_admin_owned_project_does_not_consume_user_scan_quota(client, app_module, db_session, normal_user, admin):
    project = _make_admin_project(app_module, db_session, admin)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="admin-owned")

    response = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "admin-owned"})

    payload = response.get_json()
    assert payload["counted"] is False
    assert payload["reason"] == "Admin project - unlimited scans"
    assert app_module.User.query.get(normal_user.id).scans_used == 0


def test_admin_display_count_matches_authoritative_project_count(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    _make_user_project(app_module, db_session, normal_user, name="Count One")
    _make_user_project(app_module, db_session, normal_user, name="Count Two")

    response = client.get("/admin/projects")

    assert response.status_code == 200
    assert f"Total Projects: {app_module.Project.query.count()}".encode() in response.data


def test_paid_plan_activation_preserves_plan_limits(client, app_module, normal_user, monkeypatch):
    paid_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    order = app_module.PaymentOrder(
        order_id="ORD_QUOTA_LIMITS",
        razorpay_order_id="order_quota_limits",
        user_id=normal_user.id,
        plan_id=paid_plan.id,
        amount=paid_plan.plan_amount,
        total_amount=paid_plan.effective_price,
        currency=paid_plan.currency,
        status="pending",
    )
    app_module.db.session.add(order)
    app_module.db.session.commit()
    monkeypatch.setattr(app_module, "razorpay_client", type("FakeClient", (), {
        "utility": type("FakeUtility", (), {"verify_payment_signature": lambda self, params: True})()
    })())
    monkeypatch.setattr(app_module, "send_payment_success_email", lambda user, plan, payment_order: True)
    _login_user(client, normal_user)

    response = client.post(
        "/verify-payment",
        data={
            "razorpay_payment_id": "pay_quota_limits",
            "razorpay_order_id": "order_quota_limits",
            "razorpay_signature": "sig",
        },
    )

    refreshed = app_module.User.query.get(normal_user.id)
    assert response.get_json()["success"] is True
    assert refreshed.subscription_status == "active"
    assert refreshed.subscribed_project_limit == paid_plan.total_project_limit
    assert refreshed.subscribed_scan_limit == paid_plan.total_scan_limit
    assert refreshed.projects_used == 0
    assert refreshed.scans_used == 0
    assert order.subscription_start is not None
    assert order.subscription_end is not None


@pytest.mark.xfail(strict=True, reason="Project quota uses read-check-write without row lock/atomic conditional update; two production requests at limit-1 can both pass.")
def test_concurrent_project_create_at_limit_minus_one_requires_atomic_guard():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def handle_upload():')
    end = source.index('@app.route("/project/<int:project_id>"', start)
    body = source[start:end]
    assert "with_for_update" in body or "projects_used = projects_used + 1" in body


@pytest.mark.xfail(strict=True, reason="Pair quota checks uploaded pair count before inserting rows and has no project-scoped lock or atomic pair-slot reservation.")
def test_concurrent_pair_create_at_limit_minus_one_requires_atomic_guard():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def handle_upload():')
    end = source.index('@app.route("/project/<int:project_id>"', start)
    body = source[start:end]
    assert "with_for_update" in body and "ProjectPair" in body


@pytest.mark.xfail(strict=True, reason="Scan session end checks counted then increments scans_used in separate ORM assignments; concurrent ends can double count.")
def test_concurrent_scan_end_at_limit_minus_one_requires_atomic_guard():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def scanner_session_end():')
    end = source.index('@app.route("/detect_track"', start)
    body = source[start:end]
    assert "with_for_update" in body or "scans_used = scans_used + 1" in body
