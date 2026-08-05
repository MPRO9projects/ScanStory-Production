import os
import tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage


def _generate_mp4_bytes():
    """Smallest deterministic valid MP4 this test environment can produce.

    cv2.VideoWriter's MP4 backend works without a system ffmpeg/ffprobe CLI
    (neither is installed in this environment), unlike shelling out to a
    real ffmpeg binary - see upload_validation.py's video check, which
    relies on the same cv2 backend for exactly this reason.
    """
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
        for _ in range(5):
            writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_MP4_BYTES = _generate_mp4_bytes()


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
        data["videos"].append((BytesIO(_MP4_BYTES), f"clip-{index}.mp4"))
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


def _run_competing_calls(worker, count=2):
    barrier = Barrier(count)
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(worker, barrier) for _ in range(count)]
        return [future.result() for future in futures]


def _fresh_user(app_module, db_session, user_id):
    db_session.expire_all()
    return app_module.User.query.get(user_id)


def _recreate_legacy_scan_logs_without_unique(app_module, db_session):
    db_session.execute(app_module.text("DROP TABLE scan_logs"))
    db_session.execute(app_module.text("""
        CREATE TABLE scan_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            pair_id INTEGER,
            user_id INTEGER NOT NULL,
            scan_session_id VARCHAR(100) NOT NULL,
            is_successful BOOLEAN,
            scan_type VARCHAR(50),
            counted BOOLEAN,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    """))
    db_session.commit()


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


def test_atomic_project_quota_at_limit_minus_one(app_module, db_session, normal_user):
    normal_user.subscribed_project_limit = 1
    normal_user.projects_used = 0
    db_session.commit()

    assert app_module._reserve_project_quota_atomic(normal_user) is True
    assert app_module._reserve_project_quota_atomic(normal_user) is False
    db_session.commit()

    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.projects_used == 1


def test_competing_project_quota_reservations_allow_only_one(app_module, db_session, normal_user):
    normal_user.subscribed_project_limit = 1
    normal_user.projects_used = 0
    db_session.commit()
    user_id = normal_user.id

    def reserve_project(barrier):
        with app_module.app.app_context():
            user = app_module.User.query.get(user_id)
            barrier.wait()
            reserved = app_module._reserve_project_quota_atomic(user)
            if reserved:
                app_module.db.session.commit()
            else:
                app_module.db.session.rollback()
            app_module.db.session.remove()
            return reserved

    results = _run_competing_calls(reserve_project)

    assert sorted(results) == [False, True]
    assert _fresh_user(app_module, db_session, user_id).projects_used == 1


def test_atomic_pair_quota_at_limit_minus_one(app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    _make_pair(app_module, db_session, project, index=0)

    first_ok, first_error = app_module._reserve_pair_slots_for_project(project.id, requested_pairs=1, max_pairs=2)
    assert first_ok is True
    assert first_error is None
    _make_pair(app_module, db_session, project, index=1)

    second_ok, second_error = app_module._reserve_pair_slots_for_project(project.id, requested_pairs=1, max_pairs=2)
    assert second_ok is False
    assert "maximum 2 pairs" in second_error


def test_pair_quota_behavior_uses_initial_project_upload_route(client, app_module, db_session, normal_user, monkeypatch):
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


def test_atomic_scan_quota_at_limit_minus_one(app_module, db_session, normal_user):
    normal_user.subscribed_scan_limit = 1
    normal_user.scans_used = 0
    db_session.commit()

    assert app_module._consume_scan_quota_atomic(normal_user) is True
    assert app_module._consume_scan_quota_atomic(normal_user) is False
    db_session.commit()

    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.scans_used == 1
    assert refreshed.subscription_status == "limit_reached"


def test_competing_scan_quota_reservations_allow_only_one(app_module, db_session, normal_user):
    normal_user.subscribed_scan_limit = 1
    normal_user.scans_used = 0
    db_session.commit()
    user_id = normal_user.id

    def consume_scan(barrier):
        with app_module.app.app_context():
            user = app_module.User.query.get(user_id)
            barrier.wait()
            consumed = app_module._consume_scan_quota_atomic(user)
            if consumed:
                app_module.db.session.commit()
            else:
                app_module.db.session.rollback()
            app_module.db.session.remove()
            return consumed

    results = _run_competing_calls(consume_scan)

    assert sorted(results) == [False, True]
    refreshed = _fresh_user(app_module, db_session, user_id)
    assert refreshed.scans_used == 1
    assert refreshed.subscription_status == "limit_reached"


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


def test_rollback_after_failed_project_upload_releases_project_quota(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 1
    db_session.commit()
    _login_user(client, normal_user)

    def fail_pair_reservation(*_args, **_kwargs):
        raise RuntimeError("simulated pair reservation failure")

    monkeypatch.setattr(app_module, "_reserve_pair_slots_for_project", fail_pair_reservation)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/create-project")
    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0
    assert app_module.User.query.get(normal_user.id).projects_used == 0


def test_rollback_after_failed_pair_persistence_releases_quota_and_files(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 1
    db_session.commit()
    _login_user(client, normal_user)
    # P0D: content is now validated to a temp path first, then moved into
    # place with os.replace() - only that final move is the equivalent
    # "persist to permanent location" step FileStorage.save() used to be.
    original_replace = os.replace

    def fail_video_replace(src, dst, *args, **kwargs):
        if str(dst).endswith(".mp4"):
            raise OSError("simulated video persistence failure")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_video_replace)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0
    assert app_module.User.query.get(normal_user.id).projects_used == 0
    assert not list(Path(app_module.IMAGES_DIR).glob("*.jpg"))


def test_unlimited_project_and_scan_limits_allow_usage(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    normal_user.subscribed_project_limit = 0
    normal_user.subscribed_scan_limit = 0
    normal_user.projects_used = 25
    normal_user.scans_used = 40
    db_session.commit()
    _login_user(client, normal_user)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.projects_used == 26
    assert refreshed.can_scan is True


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


def test_project_create_uses_atomic_quota_reservation():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def handle_upload():')
    end = source.index('@app.route("/project/<int:project_id>"', start)
    body = source[start:end]
    assert "_reserve_project_quota_atomic(user)" in body
    assert "user.projects_used = int(user.projects_used or 0) + 1" not in body


def test_pair_create_uses_project_scoped_pair_slot_reservation():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def handle_upload():')
    end = source.index('@app.route("/project/<int:project_id>"', start)
    body = source[start:end]
    assert "_reserve_pair_slots_for_project(project.id, len(images), max_pairs)" in body
    assert "with_for_update" in source


def test_scan_end_uses_atomic_claim_and_quota_consumption():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def scanner_session_end():')
    end = source.index('@app.route("/detect_track"', start)
    body = source[start:end]
    assert "ScanLog.counted == False" in body
    assert "_consume_scan_quota_atomic(user)" in body
    assert "user.scans_used = (user.scans_used or 0) + 1" not in body


def test_duplicate_scanner_session_end_idempotency_after_atomic_claim(client, app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="atomic-repeat")

    first = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "atomic-repeat"}).get_json()
    second = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "atomic-repeat"}).get_json()

    assert first["counted"] is True
    assert second["counted"] is False
    assert app_module.User.query.get(normal_user.id).scans_used == 1


def test_concurrent_duplicate_scanner_session_end_counts_once(app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="atomic-concurrent-repeat")
    project_id = project.id
    user_id = normal_user.id

    def end_session(barrier):
        client = app_module.app.test_client()
        barrier.wait()
        response = client.post(
            "/api/scanner/session/end",
            json={"project_id": project_id, "session_id": "atomic-concurrent-repeat"},
        )
        return response.status_code, response.get_json()

    results = _run_competing_calls(end_session)
    payloads = [payload for status, payload in results if status == 200]

    assert len(payloads) == 2
    assert sorted(payload["counted"] for payload in payloads) == [False, True]
    assert _fresh_user(app_module, db_session, user_id).scans_used == 1
    assert app_module.ScanLog.query.filter_by(scan_session_id="atomic-concurrent-repeat", counted=True).count() == 1


def test_scan_session_id_is_unique_per_owner(app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="unique-owner-session")

    duplicate = app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id,
        user_id=normal_user.id,
        scan_session_id="unique-owner-session",
        is_successful=True,
    )
    db_session.add(duplicate)

    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


def test_scanlog_uniqueness_migration_dry_run_reports_duplicates_without_changes(app_module, db_session, normal_user):
    _recreate_legacy_scan_logs_without_unique(app_module, db_session)
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    normal_user.scans_used = 5
    db_session.commit()
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="legacy-dup")
    _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="legacy-dup").counted = True
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["migrate-scanlog-session-uniqueness"])

    assert result.exit_code == 0
    assert "Mode: dry-run" in result.output
    assert "Duplicate groups: 1" in result.output
    assert "Affected rows: 2" in result.output
    assert "Groups with multiple counted rows: 0" in result.output
    assert "conflicting_scan_data=False" in result.output
    assert app_module.ScanLog.query.filter_by(scan_session_id="legacy-dup").count() == 2
    assert app_module.User.query.get(normal_user.id).scans_used == 5
    assert app_module.scan_log_session_uniqueness_report()["constraint_exists"] is False


def test_scanlog_uniqueness_migration_apply_consolidates_non_conflicting_duplicates(app_module, db_session, normal_user):
    _recreate_legacy_scan_logs_without_unique(app_module, db_session)
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    normal_user.scans_used = 9
    db_session.commit()
    first = _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="legacy-apply")
    first.is_successful = False
    first.counted = True
    second = _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="legacy-apply")
    second.is_successful = True
    second.counted = False
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["migrate-scanlog-session-uniqueness", "--apply"])

    assert result.exit_code == 0
    assert "Consolidated user_id=" in result.output
    assert "preserved_successful=True" in result.output
    assert "preserved_counted=True" in result.output
    assert "Created unique index uq_scan_logs_user_session." in result.output
    rows = app_module.ScanLog.query.filter_by(scan_session_id="legacy-apply").all()
    assert len(rows) == 1
    assert rows[0].is_successful is True
    assert rows[0].counted is True
    assert app_module.User.query.get(normal_user.id).scans_used == 9
    assert app_module.scan_log_session_uniqueness_report()["constraint_exists"] is True

    db_session.add(app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id,
        user_id=normal_user.id,
        scan_session_id="legacy-apply",
        is_successful=True,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    second_apply = app_module.app.test_cli_runner().invoke(args=["migrate-scanlog-session-uniqueness", "--apply"])
    assert second_apply.exit_code == 0
    assert "Unique index already present; no changes needed." in second_apply.output


def test_scanlog_uniqueness_migration_refuses_conflicting_duplicates_without_partial_cleanup(app_module, db_session, normal_user):
    _recreate_legacy_scan_logs_without_unique(app_module, db_session)
    project_one = _make_user_project(app_module, db_session, normal_user, name="Project One")
    pair_one = _make_pair(app_module, db_session, project_one)
    project_two = _make_user_project(app_module, db_session, normal_user, name="Project Two")
    pair_two = _make_pair(app_module, db_session, project_two)
    _make_successful_scan(app_module, db_session, project_one, pair_one, normal_user, session_id="legacy-conflict")
    _make_successful_scan(app_module, db_session, project_two, pair_two, normal_user, session_id="legacy-conflict")

    result = app_module.app.test_cli_runner().invoke(args=["migrate-scanlog-session-uniqueness", "--apply"])

    assert result.exit_code != 0
    assert "Refusing to consolidate conflicting scan log duplicates" in result.output
    assert "Groups with conflicting scan/project data: 1" in result.output
    assert app_module.ScanLog.query.filter_by(scan_session_id="legacy-conflict").count() == 2
    assert app_module.scan_log_session_uniqueness_report()["constraint_exists"] is False


def test_reconcile_quota_counters_dry_run_reports_without_repair(app_module, db_session, normal_user):
    _make_user_project(app_module, db_session, normal_user)
    normal_user.projects_used = 7
    normal_user.scans_used = 3
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-quota-counters"])

    assert result.exit_code == 0
    assert "Mode: dry-run" in result.output
    assert "Users with drift: 1" in result.output
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.projects_used == 7
    assert refreshed.scans_used == 3


def test_reconcile_quota_counters_repair_updates_stored_counters(app_module, db_session, normal_user):
    project = _make_user_project(app_module, db_session, normal_user)
    pair = _make_pair(app_module, db_session, project)
    scan = _make_successful_scan(app_module, db_session, project, pair, normal_user, session_id="repair-counted")
    scan.counted = True
    normal_user.projects_used = 9
    normal_user.scans_used = 8
    db_session.commit()

    result = app_module.app.test_cli_runner().invoke(args=["reconcile-quota-counters", "--repair"])

    assert result.exit_code == 0
    refreshed = app_module.User.query.get(normal_user.id)
    assert refreshed.projects_used == 1
    assert refreshed.scans_used == 1
