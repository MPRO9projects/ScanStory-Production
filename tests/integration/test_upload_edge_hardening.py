from io import BytesIO

from tests.security.test_upload_validation import _jpeg_bytes, _mp4_bytes


class NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None


def _patch_upload_side_effects(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _upload(client, image_name, video_name, image_bytes=None, video_bytes=None):
    if image_bytes is None:
        image_bytes = _jpeg_bytes()
    if video_bytes is None:
        video_bytes = _mp4_bytes()
    return client.post(
        "/upload",
        data={
            "name": "Upload Edge",
            "images": [(BytesIO(image_bytes), image_name)],
            "videos": [(BytesIO(video_bytes), video_name)],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_uppercase_extensions_currently_accepted(client, app_module, login_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, "TARGET.JPG", "VIDEO.MP4")
    assert response.status_code == 302
    pair = app_module.ProjectPair.query.first()
    assert pair.video_filename.endswith(".mp4")


def test_double_extension_video_stores_validated_mp4_name(client, app_module, login_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, "target.jpg", "clip.mp4.exe")
    assert response.status_code == 302
    pair = app_module.ProjectPair.query.first()
    assert pair.video_filename.endswith(".mp4")
    assert ".exe" not in pair.video_filename


def test_missing_video_extension_defaults_to_mp4(client, app_module, login_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, "target.jpg", "clip")
    assert response.status_code == 302
    pair = app_module.ProjectPair.query.first()
    assert pair.video_filename.endswith(".mp4")


def test_path_traversal_filename_not_used_for_storage(client, app_module, login_user, monkeypatch, path_traversal_filename):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, path_traversal_filename, "..\\..\\clip.mp4")
    assert response.status_code == 302
    pair = app_module.ProjectPair.query.first()
    assert ".." not in pair.image_filename
    assert ".." not in pair.video_filename


def test_empty_files_are_rejected_by_upload_validation(client, app_module, login_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, "empty.jpg", "empty.mp4", b"", b"")
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_mismatched_multiple_upload_counts_rejected(client, login_user):
    response = client.post(
        "/upload",
        data={
            "name": "Mismatch",
            "images": [(BytesIO(b"x"), "one.jpg"), (BytesIO(b"y"), "two.jpg")],
            "videos": [(BytesIO(b"z"), "one.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_configured_pair_limit_blocks_upload(client, app_module, login_user, normal_user):
    plan = app_module.SubscriptionPlan.query.get(normal_user.subscription_id)
    plan.max_pairs_per_project = 1
    app_module.db.session.commit()
    response = client.post(
        "/upload",
        data={
            "name": "Too Many",
            "images": [(BytesIO(b"x"), "one.jpg"), (BytesIO(b"y"), "two.jpg")],
            "videos": [(BytesIO(b"z"), "one.mp4"), (BytesIO(b"q"), "two.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_qr_generation_failure_still_sets_qr_path_current_behavior(client, app_module, login_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    response = _upload(client, "target.jpg", "clip.mp4")
    assert response.status_code == 302
    project = app_module.Project.query.first()
    assert project.qr_code_path == f"/qr/project_{project.id}_main.png"
