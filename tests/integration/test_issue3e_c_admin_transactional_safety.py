"""Issue 3E-C follow-up: admin upload transactional safety.

/admin/projects/upload used to commit the Project (and its service coverage)
BEFORE the pairs/PairMedia/ledger loop even started, so a failure partway
through that loop could leave a real, permanently public admin project with
zero pairs. The fix moves the commit to the end of one try/except spanning
Project creation through the last ledger row, matching the user-upload
path's existing all-or-nothing contract, and cleans up any files already
moved to permanent storage via the existing _unlink_project_media() helper
on rollback.

Media fixtures/helpers are duplicated from
tests/integration/test_issue3e_c_multi_video_upload.py for the same
documented reason that file gives: a stray global site-packages `tests`
package shadows dotted `tests.xxx` imports in this environment.
"""
import os
import tempfile
from io import BytesIO

import cv2
import numpy as np
import pytest
from PIL import Image


def _generate_mp4_bytes():
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
_MP4_BYTES_2 = _generate_mp4_bytes()


def _jpeg_bytes(width=640, height=480, shade=120):
    out = BytesIO()
    Image.new("RGB", (width, height), (shade, 80, 40)).save(out, format="JPEG")
    out.seek(0)
    return out


class NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        return None


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _patch_upload_side_effects(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *a, **k: __import__("pathlib").Path(a[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: __import__("pathlib").Path(a[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *a, **k: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *a, **k: __import__("pathlib").Path(a[3]).write_bytes(b"qr"))
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _upload_data(name, image_count, video_specs, video_target_indexes=None):
    data = {"name": name, "upload_id": f"3ec-tx-{name}"}
    data["images"] = [(_jpeg_bytes(), f"img-{i}.jpg") for i in range(image_count)]
    data["videos"] = [(BytesIO(blob), fname) for blob, fname in video_specs]
    if video_target_indexes is not None:
        data["video_target_indexes"] = [str(v) for v in video_target_indexes]
    return data


def _fail_on_nth_call(real_func, fail_at):
    """Wrapper that raises on the Nth call (1-based) and delegates to the
    real function on every other call - lets a multi-target upload succeed
    up to a deterministic point before failing, so a test can prove the
    ALREADY-persisted-in-this-request work also rolls back, not just the
    failing call's own effect."""
    state = {"n": 0}

    def wrapper(*args, **kwargs):
        state["n"] += 1
        if state["n"] == fail_at:
            raise RuntimeError("injected failure for test")
        return real_func(*args, **kwargs)

    return wrapper


# ===========================================================================
# 1-2: success paths unchanged by the transactional rewrite.
# ===========================================================================
def test_admin_one_target_one_video_success_unchanged(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_admin(client, admin)
    data = _upload_data("Admin Single Unchanged", 1, [(_MP4_BYTES, "v0.mp4")])

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code in (302, 200)

    project = app_module.Project.query.filter_by(owner_admin_id=admin.id, name="Admin Single Unchanged").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    assert pair.video_filename == f"{project.id}_0.mp4"
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1
    assert os.path.exists(os.path.join(app_module.ADMIN_IMAGES_DIR, pair.image_filename))
    assert os.path.exists(os.path.join(app_module.ADMIN_VIDEOS_DIR, pair.video_filename))


def test_admin_multi_video_success_unchanged(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_admin(client, admin)
    data = _upload_data(
        "Admin Multi Unchanged", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
        video_target_indexes=[0, 0],
    )

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code in (302, 200)

    project = app_module.Project.query.filter_by(owner_admin_id=admin.id, name="Admin Multi Unchanged").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).all()
    assert len(media_rows) == 2
    assert sum(1 for m in media_rows if m.is_default) == 1


# ===========================================================================
# 3: failure before ProjectPair completion leaves zero Project rows.
# ===========================================================================
def test_failure_before_first_pair_leaves_zero_project_rows(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    monkeypatch.setattr(
        app_module, "add_project_service_coverage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected failure for test")),
    )
    _login_admin(client, admin)
    before = app_module.Project.query.count()
    data = _upload_data("Admin Fail Before Pairs", 1, [(_MP4_BYTES, "v0.mp4")])

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    assert app_module.Project.query.count() == before


# ===========================================================================
# 4-5: failure during a LATER target's PairMedia creation rolls back every
# pair/media created earlier in the SAME request too - proves atomicity
# across the whole request, not just the failing call's own effect.
# ===========================================================================
def test_failure_during_later_pair_media_leaves_zero_project_pairs(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    real_create = app_module.create_pair_media_rows
    monkeypatch.setattr(app_module, "create_pair_media_rows", _fail_on_nth_call(real_create, fail_at=2))
    _login_admin(client, admin)
    before_pairs = app_module.ProjectPair.query.count()
    data = _upload_data(
        "Admin Fail Second Pair", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
    )

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    # The FIRST target's pair was already added to the session before the
    # second target's create_pair_media_rows raised - it must not survive.
    assert app_module.ProjectPair.query.count() == before_pairs


def test_failure_during_later_pair_media_leaves_zero_pair_media_rows(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    real_create = app_module.create_pair_media_rows
    monkeypatch.setattr(app_module, "create_pair_media_rows", _fail_on_nth_call(real_create, fail_at=2))
    _login_admin(client, admin)
    before_media = app_module.PairMedia.query.count()
    data = _upload_data(
        "Admin Fail Second Pair Media", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
    )

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    assert app_module.PairMedia.query.count() == before_media


# ===========================================================================
# 6: failure during MediaObject/ledger creation leaves no ledger rows (and
# nothing else either).
# ===========================================================================
def test_failure_during_media_object_ledger_leaves_no_ledger_rows(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    monkeypatch.setattr(
        app_module, "record_pair_media_objects",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected failure for test")),
    )
    _login_admin(client, admin)
    before_media_objects = app_module.MediaObject.query.count()
    before_pairs = app_module.ProjectPair.query.count()
    before_projects = app_module.Project.query.count()
    data = _upload_data("Admin Fail Ledger", 1, [(_MP4_BYTES, "v0.mp4")])

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    assert app_module.MediaObject.query.count() == before_media_objects
    assert app_module.ProjectPair.query.count() == before_pairs
    assert app_module.Project.query.count() == before_projects


# ===========================================================================
# 7: the per-admin project-index counter is not skipped/leaked by a rolled
# back attempt - the next SUCCESSFUL upload gets the index a failed attempt
# would otherwise have consumed.
# ===========================================================================
def test_project_index_counter_unaffected_by_rolled_back_attempt(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    monkeypatch.setattr(
        app_module, "add_project_service_coverage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected failure for test")),
    )
    _login_admin(client, admin)
    failing_data = _upload_data("Admin Counter Fail", 1, [(_MP4_BYTES, "v0.mp4")])
    response = client.post("/admin/projects/upload", data=failing_data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert app_module.Project.query.filter_by(owner_admin_id=admin.id).count() == 0

    monkeypatch.undo()
    _patch_upload_side_effects(app_module, monkeypatch)
    succeeding_data = _upload_data("Admin Counter Success", 1, [(_MP4_BYTES, "v0.mp4")])
    response = client.post("/admin/projects/upload", data=succeeding_data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code in (302, 200)

    project = app_module.Project.query.filter_by(owner_admin_id=admin.id, name="Admin Counter Success").one()
    assert project.user_project_index == 1


# ===========================================================================
# 8: files already moved to permanent storage during the failed request are
# cleaned up on rollback.
# ===========================================================================
def test_newly_written_files_are_cleaned_on_rollback(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    real_create = app_module.create_pair_media_rows
    monkeypatch.setattr(app_module, "create_pair_media_rows", _fail_on_nth_call(real_create, fail_at=2))
    _login_admin(client, admin)

    from sqlalchemy import func
    next_project_id = (db_session.query(func.max(app_module.Project.id)).scalar() or 0) + 1

    data = _upload_data(
        "Admin Fail Cleanup", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
    )
    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    # Neither target's files should survive - target 0's succeeded before
    # target 1 failed, and both must be cleaned up, not just target 1's.
    for target_index in (0, 1):
        assert not os.path.exists(os.path.join(app_module.ADMIN_IMAGES_DIR, f"{next_project_id}_{target_index}.jpg"))
        assert not os.path.exists(os.path.join(app_module.ADMIN_VIDEOS_DIR, f"{next_project_id}_{target_index}.mp4"))


# ===========================================================================
# 9: a successful admin upload retains all of its files (the cleanup path
# must never fire on the success path).
# ===========================================================================
def test_successful_admin_upload_retains_all_files(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_admin(client, admin)
    data = _upload_data(
        "Admin Retains Files", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
    )
    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code in (302, 200)

    project = app_module.Project.query.filter_by(owner_admin_id=admin.id, name="Admin Retains Files").one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 2
    for pair in pairs:
        assert os.path.exists(os.path.join(app_module.ADMIN_IMAGES_DIR, pair.image_filename))
        assert os.path.exists(os.path.join(app_module.ADMIN_VIDEOS_DIR, pair.video_filename))


# ===========================================================================
# 10: the user-upload path's own behavior is unaffected by this admin-only
# change (it shares helper functions, not the transaction boundary itself).
# ===========================================================================
def test_user_upload_behavior_unchanged(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("User Upload Unaffected", 1, [(_MP4_BYTES, "v0.mp4")])

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="User Upload Unaffected").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    assert pair.video_filename == f"{project.id}_0.mp4"
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1
