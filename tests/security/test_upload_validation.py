"""P0D: production-grade upload content validation.

Covers /upload (the primary project-creation path). Every accepted image
must decode as a real JPEG/PNG; every accepted video must have a real,
readable video stream in an MP4 container. Nothing here trusts a filename
extension, Content-Type header, or FileStorage.mimetype.
"""
import importlib
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Fixture-generation helpers - no committed binaries, everything built here.
# ---------------------------------------------------------------------------

def _jpeg_bytes(width=640, height=480):
    out = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(out, format="JPEG")
    return out.getvalue()


def _png_bytes(width=640, height=480):
    out = BytesIO()
    Image.new("RGB", (width, height), (40, 120, 80)).save(out, format="PNG")
    return out.getvalue()


_MP4_CACHE = {}


def _mp4_bytes(width=64, height=64, frames=5):
    """Smallest deterministic valid MP4 this test environment can produce.

    cv2's bundled MP4 backend works with no system ffmpeg/ffprobe CLI on
    PATH (confirmed absent in this environment) - the same backend
    upload_validation.py relies on to verify a real video stream exists.
    """
    key = (width, height, frames)
    if key not in _MP4_CACHE:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
            for _ in range(frames):
                writer.write(np.zeros((height, width, 3), dtype=np.uint8))
            writer.release()
            with open(path, "rb") as fh:
                _MP4_CACHE[key] = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return _MP4_CACHE[key]


def _upload_data(image_bytes, video_bytes, image_name="marker.jpg", video_name="clip.mp4", name="Upload Validation Project"):
    return {
        "name": name,
        "upload_id": f"uv-{name}",
        "images": [(BytesIO(image_bytes), image_name)],
        "videos": [(BytesIO(video_bytes), video_name)],
        "marker_0_mode": "crop",
        "marker_0_crop_x": "0.1",
        "marker_0_crop_y": "0.1",
        "marker_0_crop_width": "0.6",
        "marker_0_crop_height": "0.6",
        "marker_0_rotation": "0",
        "marker_0_original_width": "640",
        "marker_0_original_height": "480",
        "marker_0_processed_width": "520",
        "marker_0_processed_height": "420",
        "marker_0_source_size_bytes": "100000",
        "marker_0_processed_size_bytes": "90000",
        "marker_0_display_orientation": "landscape",
    }


def _post_upload(client, **kwargs):
    data = _upload_data(**kwargs)
    return client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)


# ---------------------------------------------------------------------------
# 1-3: valid uploads accepted
# ---------------------------------------------------------------------------

def test_valid_jpeg_accepted(client, app_module, login_user):
    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 1
    pair = app_module.ProjectPair.query.first()
    assert pair.processing_status == "uploaded"


def test_valid_png_accepted(client, app_module, login_user):
    response = _post_upload(client, image_bytes=_png_bytes(), video_bytes=_mp4_bytes(), image_name="marker.png")
    assert response.status_code == 302
    assert app_module.Project.query.count() == 1


def test_valid_supported_video_accepted(client, app_module, login_user):
    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes(frames=8))
    assert response.status_code == 302
    pair = app_module.ProjectPair.query.first()
    assert pair.video_filename.endswith(".mp4")
    assert os.path.exists(os.path.join(app_module.VIDEOS_DIR, pair.video_filename))


# ---------------------------------------------------------------------------
# 4-7: content/extension mismatch is rejected
# ---------------------------------------------------------------------------

def test_executable_renamed_jpg_rejected(client, app_module, login_user):
    response = _post_upload(client, image_bytes=b"MZ\x90\x00\x03\x00\x00\x00fake-pe-header", video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_html_renamed_png_rejected(client, app_module, login_user):
    response = _post_upload(
        client,
        image_bytes=b"<html><body><script>alert(1)</script></body></html>",
        video_bytes=_mp4_bytes(),
        image_name="marker.png",
    )
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_zip_renamed_mp4_rejected(client, app_module, login_user):
    response = _post_upload(
        client,
        image_bytes=_jpeg_bytes(),
        video_bytes=b"PK\x03\x04\x14\x00\x00\x00\x00\x00fake-zip-payload",
    )
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_image_renamed_mp4_rejected(client, app_module, login_user):
    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_jpeg_bytes(), video_name="clip.mp4")
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


# ---------------------------------------------------------------------------
# 8-11: malformed / truncated / empty content is rejected
# ---------------------------------------------------------------------------

def test_malformed_image_rejected(client, app_module, login_user):
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * 200  # valid JPEG magic, garbage after
    response = _post_upload(client, image_bytes=payload, video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_truncated_image_rejected(client, app_module, login_user):
    full = _jpeg_bytes(1200, 900)
    truncated = full[: len(full) // 3]
    response = _post_upload(client, image_bytes=truncated, video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_malformed_or_truncated_video_rejected(client, app_module, login_user):
    full = _mp4_bytes()
    truncated = full[:32]  # keeps the ftyp box, no real frame data follows
    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=truncated)
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_empty_upload_rejected(client, app_module, login_user):
    response = _post_upload(client, image_bytes=b"", video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


# ---------------------------------------------------------------------------
# 12-13: oversized uploads rejected
# ---------------------------------------------------------------------------

def test_oversize_image_rejected(client, app_module, login_user, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_IMAGE_SIZE", 100)
    response = _post_upload(client, image_bytes=_jpeg_bytes(1200, 900), video_bytes=_mp4_bytes())
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_oversize_video_rejected(client, app_module, login_user, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_VIDEO_SIZE", 100)
    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes(frames=8))
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


# ---------------------------------------------------------------------------
# 14: path traversal filenames cannot escape storage
# ---------------------------------------------------------------------------

def test_path_traversal_filename_cannot_escape_storage_directory(client, app_module, login_user):
    response = _post_upload(
        client,
        image_bytes=_jpeg_bytes(),
        video_bytes=_mp4_bytes(),
        image_name="../../../../evil.jpg",
        video_name="..\\..\\evil.mp4",
    )
    assert response.status_code == 302
    assert app_module.Project.query.count() == 1
    project = app_module.Project.query.first()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).first()
    # Storage filenames are always server-generated - the malicious names
    # never reach the filesystem, and no file appears outside IMAGES_DIR.
    assert pair.image_filename == f"{project.id}_0.jpg"
    assert ".." not in pair.image_filename
    assert not Path("evil.jpg").exists()
    assert not (Path(app_module.IMAGES_DIR).parent / "evil.jpg").exists()


# ---------------------------------------------------------------------------
# 15-18: rejected uploads touch neither DB, quota, nor leave temp files
# ---------------------------------------------------------------------------

def test_failed_validation_creates_no_project_row(client, app_module, login_user):
    before = app_module.Project.query.count()
    _post_upload(client, image_bytes=b"not-an-image", video_bytes=_mp4_bytes())
    assert app_module.Project.query.count() == before


def test_failed_validation_creates_no_projectpair_rows(client, app_module, login_user):
    before = app_module.ProjectPair.query.count()
    _post_upload(client, image_bytes=b"not-an-image", video_bytes=_mp4_bytes())
    assert app_module.ProjectPair.query.count() == before


def test_failed_validation_consumes_no_project_quota(client, app_module, login_user):
    before = app_module.User.query.get(login_user.id).projects_used
    _post_upload(client, image_bytes=b"not-an-image", video_bytes=_mp4_bytes())
    assert app_module.User.query.get(login_user.id).projects_used == before


def test_temporary_files_are_removed_after_rejection(client, app_module, login_user):
    _post_upload(client, image_bytes=b"not-an-image", video_bytes=_mp4_bytes())
    leftovers = list(Path(app_module.TMP_UPLOADS_DIR).glob("upload_*"))
    assert leftovers == []


def test_temporary_files_are_removed_after_success(client, app_module, login_user):
    _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes())
    leftovers = list(Path(app_module.TMP_UPLOADS_DIR).glob("upload_*"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# 19: failure after validation but before DB commit removes saved media
# ---------------------------------------------------------------------------

def test_failure_after_validation_but_before_commit_removes_saved_media(client, app_module, login_user, monkeypatch):
    original_replace = os.replace

    def fail_on_video_move(src, dst, *args, **kwargs):
        if str(dst).endswith(".mp4"):
            raise OSError("simulated failure between validation and DB commit")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_on_video_move)
    before_images = set(Path(app_module.IMAGES_DIR).glob("*.jpg"))

    response = _post_upload(client, image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes())

    assert response.status_code == 302
    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0
    after_images = set(Path(app_module.IMAGES_DIR).glob("*.jpg"))
    assert after_images == before_images  # the partially-moved image was cleaned up too


# ---------------------------------------------------------------------------
# 20: normal multi-pair upload behavior still works end to end
# ---------------------------------------------------------------------------

def test_valid_existing_project_upload_behavior_remains_working(client, app_module, login_user, monkeypatch):
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 10)
    login_user.subscribed_project_limit = 10
    app_module.db.session.commit()

    data = {"name": "Multi Pair Project", "upload_id": "uv-multi", "images": [], "videos": []}
    for index in range(2):
        data["images"].append((BytesIO(_jpeg_bytes()), f"marker-{index}.jpg"))
        data["videos"].append((BytesIO(_mp4_bytes()), f"clip-{index}.mp4"))
        for key, value in {
            "mode": "crop", "crop_x": "0.1", "crop_y": "0.1", "crop_width": "0.6", "crop_height": "0.6",
            "rotation": "0", "original_width": "640", "original_height": "480",
            "processed_width": "520", "processed_height": "420",
            "source_size_bytes": "100000", "processed_size_bytes": "90000",
            "display_orientation": "landscape",
        }.items():
            data[f"marker_{index}_{key}"] = value

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    project = app_module.Project.query.order_by(app_module.Project.id.desc()).first()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert len(pairs) == 2
    assert all(p.processing_status == "uploaded" for p in pairs)


# ---------------------------------------------------------------------------
# 21: CSRF remains enforced on the upload route
# ---------------------------------------------------------------------------

def _reimport_app_with_real_csrf(monkeypatch, tmp_path):
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{(tmp_path / 'upload-csrf.db').as_posix()}")
    monkeypatch.setenv("SCANSTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCANSTORY_ADMIN_DATA_DIR", str(tmp_path / "data_admin"))
    monkeypatch.setenv("SCANSTORY_STATIC_UPLOADS_DIR", str(tmp_path / "static_uploads"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "upload-csrf-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    for name in list(sys.modules):
        if name == "app":
            sys.modules.pop(name)
    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True)  # deliberately NOT forcing WTF_CSRF_ENABLED=False
    return app_module


def test_csrf_remains_enforced_on_upload_route(monkeypatch, tmp_path):
    app_module = _reimport_app_with_real_csrf(monkeypatch, tmp_path)
    try:
        with app_module.app.app_context():
            app_module.db.create_all()
            app_module.bootstrap_database()
            app_module.db.session.commit()
            plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
            from werkzeug.security import generate_password_hash
            user = app_module.User(
                email="upload-csrf@example.com",
                password_hash=generate_password_hash("password123"),
                is_verified=True,
                subscription_id=plan.id,
                subscription_status="trial",
                subscribed_project_limit=plan.total_project_limit,
                subscribed_scan_limit=plan.total_scan_limit,
            )
            app_module.db.session.add(user)
            app_module.db.session.commit()
            user_id = user.id

        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id

        data = _upload_data(image_bytes=_jpeg_bytes(), video_bytes=_mp4_bytes())
        response = client.post("/upload", data=data, content_type="multipart/form-data")
        assert response.status_code == 400  # CSRF rejection, not a validation rejection
        with app_module.app.app_context():
            assert app_module.Project.query.count() == 0
    finally:
        for name in list(sys.modules):
            if name == "app":
                sys.modules.pop(name)
