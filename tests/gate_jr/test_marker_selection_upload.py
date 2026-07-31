from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from werkzeug.security import generate_password_hash


class NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        return None


def _jpeg_bytes(width=640, height=480, color=(160, 80, 40)):
    out = BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="JPEG", quality=88)
    out.seek(0)
    return out


def _upload_data(name="Marker Project", modes=("crop",), widths=(640,)):
    data = {"name": name, "upload_id": f"upload-{name}"}
    images = []
    videos = []
    for index, mode in enumerate(modes):
        width = widths[index] if index < len(widths) else widths[-1]
        images.append((_jpeg_bytes(width, 480), f"marker-{index}.jpg"))
        videos.append((BytesIO(b"video"), f"clip-{index}.mp4"))
        data[f"marker_{index}_mode"] = mode
        data[f"marker_{index}_crop_x"] = "0.1" if mode == "crop" else "0"
        data[f"marker_{index}_crop_y"] = "0.2" if mode == "crop" else "0"
        data[f"marker_{index}_crop_width"] = "0.5" if mode == "crop" else "1"
        data[f"marker_{index}_crop_height"] = "0.4" if mode == "crop" else "1"
        data[f"marker_{index}_rotation"] = "90" if index == 0 else "0"
        data[f"marker_{index}_original_width"] = str(width)
        data[f"marker_{index}_original_height"] = "480"
        data[f"marker_{index}_processed_width"] = "520"
        data[f"marker_{index}_processed_height"] = "420"
        data[f"marker_{index}_source_size_bytes"] = "1000000"
        data[f"marker_{index}_processed_size_bytes"] = "120000"
        data[f"marker_{index}_display_orientation"] = "landscape" if width > 480 else "portrait"
    data["images"] = images
    data["videos"] = videos
    return data


def _patch_upload_processing(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: Path(args[3]).write_bytes(b"qr") if len(args) > 3 else None)
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def test_create_project_ui_defaults_to_crop_and_allows_full_image():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "markerMode: 'crop'" in html
    assert "Crop marker area" in html
    assert "Use full image as marker" in html
    assert "This area will become the marker" in html
    assert "CLIENT PREP START" in html
    assert "CLIENT IMAGE COMPRESS DONE" in html
    assert "CLIENT UPLOAD PROGRESS" in html
    assert "video-meta-${pairId}" in html
    assert "VIDEO_WARNING_CONFIG" in html
    assert "VIDEO CLIENT SELECTED" in html
    assert "VIDEO METADATA READY" in html
    assert "VIDEO CLIENT UPLOAD START" in html
    assert "VIDEO CLIENT PROGRESS" in html
    assert "VIDEO CLIENT UPLOAD RESPONSE" in html
    assert "Metadata unavailable" in html
    assert "Large file" in html
    assert "Very large file" in html
    assert "Unsupported type" in html
    assert "uploadActive = true" in html
    assert "if (uploadActive) return" in html
    assert "estimated_remaining_seconds" in html
    assert "marker_${index}_crop_x" in html
    assert "MAX_PAIRS_PER_PROJECT = (IS_ADMIN || IS_DEV_TEST) ? Infinity" in html


def test_marker_mode_is_stored_per_pair_and_mixed_modes_work(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)

    response = client.post(
        "/upload",
        data=_upload_data(modes=("crop", "full_image"), widths=(480, 800)),
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    pairs = app_module.ProjectPair.query.order_by(app_module.ProjectPair.pair_index).all()
    assert [pair.marker_mode for pair in pairs] == ["crop", "full_image"]
    assert pairs[0].marker_crop_x == 0.1
    assert pairs[0].marker_crop_y == 0.2
    assert pairs[0].marker_crop_width == 0.5
    assert pairs[0].marker_crop_height == 0.4
    assert pairs[0].marker_rotation == 90
    assert pairs[0].marker_original_width == 480
    assert pairs[0].marker_processed_width == 520
    assert pairs[0].marker_processed_size_bytes == 120000
    assert pairs[1].marker_crop_x == 0
    assert pairs[1].marker_crop_y == 0
    assert pairs[1].marker_crop_width == 1
    assert pairs[1].marker_crop_height == 1


def test_video_server_timing_logs_use_same_upload_id(client, app_module, login_user, monkeypatch):
    logs = []
    _patch_upload_processing(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "_upload_log", lambda stage, upload_id, **fields: logs.append((stage, upload_id, fields)))

    response = client.post(
        "/upload",
        data=_upload_data(name="video-timing", modes=("crop",)),
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    stages = [stage for stage, _upload_id, _fields in logs]
    assert "VIDEO SERVER REQUEST ENTER" in stages
    assert "VIDEO SERVER BODY READY" in stages
    assert "VIDEO SERVER PERSIST START" in stages
    assert "VIDEO SERVER PERSIST DONE" in stages
    assert {upload_id for _stage, upload_id, _fields in logs} == {"upload-video-timing"}

    body_ready = next(fields for stage, _upload_id, fields in logs if stage == "VIDEO SERVER BODY READY")
    persist_done = next(fields for stage, _upload_id, fields in logs if stage == "VIDEO SERVER PERSIST DONE")
    assert body_ready["content_length"] is not None
    assert body_ready["body_duration_ms"] >= 0
    assert persist_done["video_size"] == len(b"video")
    assert persist_done["persistence_duration_ms"] >= 0
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert "VIDEO BG START" in source
    assert "VIDEO BG DONE" in source
    assert "background_duration_ms" in source


def test_one_two_and_three_pair_uploads_remain_compatible(client, app_module, login_user, monkeypatch):
    login_user.subscribed_project_limit = 10
    app_module.db.session.commit()
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 10)

    for pair_count in (1, 2, 3):
        _patch_upload_processing(app_module, monkeypatch)
        response = client.post(
            "/upload",
            data=_upload_data(name=f"pairs-{pair_count}", modes=tuple(["crop"] * pair_count)),
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        assert response.status_code == 302
        project = app_module.Project.query.order_by(app_module.Project.id.desc()).first()
        pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
        assert len(pairs) == pair_count


def test_legacy_upload_without_marker_metadata_behaves_as_full_image(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)

    response = client.post(
        "/upload",
        data={
            "name": "Legacy Upload",
            "images": [(_jpeg_bytes(), "legacy.jpg")],
            "videos": [(BytesIO(b"video"), "legacy.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    pair = app_module.ProjectPair.query.one()
    assert pair.marker_mode == "full_image"
    assert pair.marker_crop_x == 0
    assert pair.marker_crop_width == 1
    assert pair.marker_display_orientation == "legacy"


def test_invalid_crop_bounds_and_minimum_size_are_rejected(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)
    bad_bounds = _upload_data()
    bad_bounds["marker_0_crop_x"] = "0.8"
    bad_bounds["marker_0_crop_width"] = "0.4"

    response = client.post("/upload", data=bad_bounds, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0

    too_small = _upload_data()
    too_small["marker_0_processed_width"] = "120"
    response = client.post("/upload", data=too_small, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_roi_coverage_is_separate_from_full_frame_position(app_module):
    marker_w, marker_h = 500, 300
    frame_w, frame_h = 1200, 900
    src = np.array(
        [[40, 40], [250, 35], [460, 45], [55, 150], [250, 150], [445, 155], [35, 260], [250, 265], [465, 255]],
        dtype=np.float32,
    )
    small_roi = np.array([[80, 70], [260, 75], [255, 180], [85, 175]], dtype=np.float32)
    h_matrix = cv2.getPerspectiveTransform(
        np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        small_roi,
    )
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(src, dst, h_matrix, mask, marker_w, marker_h, frame_w, frame_h, scale=1.0)

    assert ok
    assert quality["projected_roi_grid_cells"] >= 3
    assert quality["frame_grid_cells"] <= quality["projected_roi_grid_cells"]


def test_dev_test_payment_route_is_blocked_without_payment_order(client, app_module, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    app_module._seed_dev_test_users()
    user = app_module.User.query.filter_by(email="scanstorytest01@gmail.com").one()
    paid_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id

    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["success"] is False
    assert "Payment is disabled" in payload["error"]
    assert app_module.PaymentOrder.query.count() == 0


def test_two_test_user_uploads_remain_isolated_with_unique_upload_ids(app_module, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    _patch_upload_processing(app_module, monkeypatch)
    app_module._seed_dev_test_users()
    users = [
        app_module.User.query.filter_by(email="scanstorytest01@gmail.com").one(),
        app_module.User.query.filter_by(email="scanstorytest02@gmail.com").one(),
    ]

    def submit(index):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = users[index].id
        response = client.post(
            "/upload",
            data=_upload_data(name=f"Concurrent {index}", modes=("crop", "full_image")),
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        return response.status_code

    statuses = [submit(0), submit(1)]

    assert statuses == [302, 302]
    projects = app_module.Project.query.order_by(app_module.Project.id).all()
    assert len(projects) == 2
    assert {project.owner_user_id for project in projects} == {user.id for user in users}
    for project in projects:
        pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
        assert len(pairs) == 2
        assert [pair.marker_mode for pair in pairs] == ["crop", "full_image"]
        for pair in pairs:
          assert pair.image_filename.startswith(f"{project.id}_")
          assert pair.processing_status == "uploaded"
