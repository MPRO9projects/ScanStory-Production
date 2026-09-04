"""Issue 3E-C: backend multi-video-per-target upload + entitlement
enforcement.

images = [target images]; videos = [ALL uploaded videos]; the optional
parallel video_target_indexes[] says which target each video belongs to.
Absent + equal counts infers the legacy 1:1 mapping unchanged.

Media fixtures are duplicated from tests/integration/test_quota_characterization.py
for the same documented reason that file gives: a stray global site-packages
`tests` package shadows dotted `tests.xxx` imports in this environment.

Scope: backend only. No scanner UI, no media chooser, no Creator "+ Add
Video" control, no Fast Video - none of that exists yet.
"""
import os
import tempfile
from io import BytesIO

import cv2
import numpy as np
import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# Media + HTTP helpers
# ---------------------------------------------------------------------------
def _generate_mp4_bytes(seed=None):
    """seed=None (legacy default) reproduces the original all-zero-frame
    behavior exactly - _MP4_BYTES below still depends on that. A given seed
    uses per-pixel noise instead: two adjacent/near-identical flat-fill clips
    were found (universal-video-duplicate-rule pass, 2026-08-31) to compress
    to byte-IDENTICAL mp4v output for a tiny 64x64/5-frame clip, silently
    defeating any test that relies on two fixtures being genuinely distinct
    content. Noise, keyed by seed, does not have that failure mode."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
        if seed is None:
            for _ in range(5):
                writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
        else:
            rng = np.random.default_rng(seed)
            for _ in range(5):
                writer.write(rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_MP4_BYTES = _generate_mp4_bytes()
_MP4_BYTES_2 = _generate_mp4_bytes(seed=2)
_MP4_BYTES_3 = _generate_mp4_bytes(seed=3)


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


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _patch_upload_side_effects(app_module, monkeypatch, feature_call_counter=None):
    if feature_call_counter is not None:
        def _count_extract(*args, **kwargs):
            feature_call_counter.append(1)
            from pathlib import Path
            Path(args[1]).write_bytes(b"npz")
        monkeypatch.setattr(app_module, "extract_features_multi", _count_extract)
    else:
        monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: __import__("pathlib").Path(a[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: __import__("pathlib").Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: __import__("pathlib").Path(args[3]).write_bytes(b"qr"))
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _marker_fields(index, prefix="marker"):
    return {
        f"{prefix}_{index}_mode": "crop",
        f"{prefix}_{index}_crop_x": "0.1",
        f"{prefix}_{index}_crop_y": "0.1",
        f"{prefix}_{index}_crop_width": "0.6",
        f"{prefix}_{index}_crop_height": "0.6",
        f"{prefix}_{index}_rotation": "0",
        f"{prefix}_{index}_original_width": "640",
        f"{prefix}_{index}_original_height": "480",
        f"{prefix}_{index}_processed_width": "520",
        f"{prefix}_{index}_processed_height": "420",
        f"{prefix}_{index}_source_size_bytes": "100000",
        f"{prefix}_{index}_processed_size_bytes": "90000",
        f"{prefix}_{index}_display_orientation": "landscape",
    }


def _upload_data(name, image_count, video_specs, video_target_indexes=None):
    """video_specs: list of (bytes, filename) for every uploaded video, in
    upload order. video_target_indexes: optional list of ints (as strings).

    Each image gets a DISTINCT shade (_jpeg_bytes(shade=...)) - a fixed
    default shade for every pair would make image_count>1 targets byte-
    identical, which the (correct, pre-existing) uq_project_pair_image_hash
    constraint rejects as a real duplicate target. That masked two tests in
    this file entirely (universal-video-duplicate-rule pass, 2026-08-31) -
    both failed on this constraint, not on anything video-related."""
    data = {"name": name, "upload_id": f"3ec-{name}"}
    data["images"] = [(_jpeg_bytes(shade=40 + i * 30), f"img-{i}.jpg") for i in range(image_count)]
    data["videos"] = [(BytesIO(blob), fname) for blob, fname in video_specs]
    for i in range(image_count):
        data.update(_marker_fields(i))
    if video_target_indexes is not None:
        data["video_target_indexes"] = [str(v) for v in video_target_indexes]
    return data


def _enable_multi_video(db_session, plan, max_videos=5):
    plan.allow_multi_video_per_target = True
    plan.max_videos_per_target = max_videos
    db_session.commit()


# ===========================================================================
# 1-2: legacy 1:1 contract, unchanged.
# ===========================================================================
def test_one_image_one_video_remains_unchanged(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("Legacy Single", 1, [(_MP4_BYTES, "v0.mp4")])

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    assert pair.video_filename == f"{project.id}_0.mp4"

    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).all()
    assert len(media_rows) == 1
    assert media_rows[0].is_default is True
    assert media_rows[0].sort_order == 0
    assert media_rows[0].video_filename == pair.video_filename


def test_legacy_no_index_multi_pair_upload_remains_unchanged(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data(
        "Legacy Multi Pair", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert len(pairs) == 2
    for pair in pairs:
        assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1


# ===========================================================================
# 3-4, 9-13: multi-video when entitled.
# ===========================================================================
def test_one_image_two_videos_works_when_entitled(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    data = _upload_data(
        "Two Videos One Target", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
        video_target_indexes=[0, 0],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    # 9: ONE ProjectPair per target, never one per video.
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 1
    pair = pairs[0]

    # 10: same target creates as many PairMedia rows as videos assigned to it.
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert len(media_rows) == 2

    # 11: exactly one default.
    defaults = [m for m in media_rows if m.is_default]
    assert len(defaults) == 1
    assert defaults[0].sort_order == 0

    # 12: sort_order is 0, 1.
    assert [m.sort_order for m in media_rows] == [0, 1]

    # 13: legacy ProjectPair fields mirror the default media only.
    assert pair.video_filename == media_rows[0].video_filename
    assert pair.original_video_name == media_rows[0].original_video_name
    assert pair.video_size == media_rows[0].video_size
    assert pair.video_filename != media_rows[1].video_filename


def test_one_image_n_videos_works_up_to_max(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=3)
    _login_user(client, normal_user)
    data = _upload_data(
        "Three Videos At Max", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4"), (_MP4_BYTES_3, "v2.mp4")],
        video_target_indexes=[0, 0, 0],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 3


# ===========================================================================
# 5-8: entitlement/mapping rejections. All-or-nothing: nothing persisted.
# ===========================================================================
def test_feature_disabled_rejects_second_video(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    assert normal_user.subscription_plan.allow_multi_video_per_target is False
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Not Entitled", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
        video_target_indexes=[0, 0],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"Multiple videos per target" in response.data
    assert app_module.Project.query.count() == before_projects


def test_exceeding_max_rejects_upload(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=2)
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Over Limit", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4"), (_MP4_BYTES, "v2.mp4")],
        video_target_indexes=[0, 0, 0],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"limit of 2 videos per target" in response.data
    assert app_module.Project.query.count() == before_projects


def test_invalid_target_index_rejected(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Bad Index", 1,
        [(_MP4_BYTES, "v0.mp4")],
        video_target_indexes=[5],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"invalid target" in response.data
    assert app_module.Project.query.count() == before_projects


def test_missing_mapping_rejected_when_counts_differ(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Missing Mapping", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4"), (_MP4_BYTES, "v2.mp4")],
        video_target_indexes=None,
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"needs at least one video" in response.data
    assert app_module.Project.query.count() == before_projects


# ===========================================================================
# 14: target feature generation happens once per ProjectPair, never once
# per video.
# ===========================================================================
def test_target_feature_generation_happens_once_per_target(client, app_module, db_session, normal_user, monkeypatch):
    calls = []
    _patch_upload_side_effects(app_module, monkeypatch, feature_call_counter=calls)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    data = _upload_data(
        "Feature Once", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4"), (_MP4_BYTES_3, "v2.mp4")],
        video_target_indexes=[0, 0, 0],
    )
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1

    from processing_operations import run_processing_job

    job = app_module._schedule_project_pair_processing(project.id)
    assert job is not None
    result = run_processing_job(job.id)
    assert result["ok"] is True
    # One target (regardless of its 3 videos) -> exactly one feature
    # extraction call, never once per video.
    assert len(calls) == 1


# ===========================================================================
# 15: different targets + same physical input video content stays independent.
# ===========================================================================
def test_different_targets_same_physical_video_content_remains_valid(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    data = _upload_data(
        "Shared Content", 2,
        [(_MP4_BYTES, "same.mp4"), (_MP4_BYTES, "same.mp4")],
        video_target_indexes=[0, 1],
    )

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert len(pairs) == 2
    assert pairs[0].video_filename != pairs[1].video_filename
    for pair in pairs:
        assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1


# ===========================================================================
# 16: user/admin parity.
# ===========================================================================
def test_admin_multi_video_upload_parity(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_admin(client, admin)
    data = _upload_data(
        "Admin Multi Video", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
        video_target_indexes=[0, 0],
    )

    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code in (302, 200)

    project = app_module.Project.query.filter_by(owner_admin_id=admin.id, name="Admin Multi Video").one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 1
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pairs[0].id).all()
    assert len(media_rows) == 2
    assert sum(1 for m in media_rows if m.is_default) == 1


# ===========================================================================
# 17: a rejected multi-video upload leaves no orphan rows.
# ===========================================================================
def test_failed_multi_video_upload_leaves_no_orphan_pair_media(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=1)
    _login_user(client, normal_user)
    before_media = app_module.PairMedia.query.count()
    before_pairs = app_module.ProjectPair.query.count()
    before_projects = app_module.Project.query.count()

    data = _upload_data(
        "Should Fail", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")],
        video_target_indexes=[0, 0],
    )
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"limit of 1 videos per target" in response.data

    assert app_module.Project.query.count() == before_projects
    assert app_module.ProjectPair.query.count() == before_pairs
    assert app_module.PairMedia.query.count() == before_media


# ===========================================================================
# 18: storage accounted exactly once per physical uploaded video.
# ===========================================================================
def test_storage_accounted_once_per_physical_uploaded_video(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    # 3 genuinely distinct videos - universal-video-duplicate-rule pass now
    # blocks byte-identical videos within the same target, so this test's own
    # "3 uploaded videos -> 3 ledger rows" storage-accounting assertion needs
    # 3 real distinct physical files, not v2 silently reusing v0's bytes.
    data = _upload_data(
        "Storage Once", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4"), (_MP4_BYTES_3, "v2.mp4")],
        video_target_indexes=[0, 0, 0],
    )
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    video_media_objects = app_module.MediaObject.query.filter_by(
        project_id=project.id, media_role=app_module._storage.MEDIA_ROLE_VIDEO,
    ).all()
    assert len(video_media_objects) == 3

    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).all()
    total_video_bytes_on_disk = sum(m.video_size for m in media_rows)
    total_billed_video_bytes = sum(mo.size_bytes for mo in video_media_objects)
    assert total_billed_video_bytes == total_video_bytes_on_disk


# ===========================================================================
# Universal video duplicate enforcement pass (2026-08-31) - the rule must not
# exist only in Edit: a project must never be CREATED with an illegal
# duplicate in the first place. handle_upload had no video-duplicate guard of
# any kind before this pass (image duplicates were already caught by the
# uq_project_pair_image_hash DB constraint; videos had nothing at all).
# ===========================================================================

def test_initial_create_duplicate_video_within_same_target_is_blocked(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Dup Within Target", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES, "v1.mp4")],  # same exact bytes, same target
        video_target_indexes=[0, 0],
    )
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"already added to this target" in response.data
    assert app_module.Project.query.count() == before_projects, "no project should exist with an illegal duplicate"


def test_initial_create_same_video_across_different_targets_still_allowed(client, app_module, db_session, normal_user, monkeypatch):
    """Regression guard (critical per the universal-video-duplicate-rule
    brief): the SAME video content under two DIFFERENT targets in one
    creation batch must remain valid - the new per-target scope must never
    widen to (project_id, video_hash)."""
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data(
        "Same Video Diff Targets", 2,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES, "v1.mp4")],
        video_target_indexes=[0, 1],
    )
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 2


def _direct_qr_upload_data(name, video_specs):
    data = {
        "name": name, "upload_id": f"dqr-{name}",
        "experience_type": "direct_qr", "playback_mode": "direct",
        "images": [],
        "videos": [(BytesIO(blob), fname) for blob, fname in video_specs],
    }
    return data


def test_direct_qr_initial_create_duplicate_video_in_playlist_is_blocked(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    before_projects = app_module.Project.query.count()
    data = _direct_qr_upload_data("DQR Dup", [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES, "v1.mp4")])
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"already part of this ScanStory" in response.data
    assert app_module.Project.query.count() == before_projects, "no project should exist with an illegal duplicate"


def test_direct_qr_initial_create_unique_videos_allowed(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _direct_qr_upload_data("DQR Unique", [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")])
    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, experience_type="direct_qr").one()
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert len(pairs) == 2
    assert pairs[0].video_filename != pairs[1].video_filename


def test_admin_initial_create_duplicate_video_within_same_target_is_blocked(client, app_module, db_session, admin, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_admin(client, admin)
    before_projects = app_module.Project.query.count()
    data = _upload_data(
        "Admin Dup Within Target", 1,
        [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES, "v1.mp4")],
        video_target_indexes=[0, 0],
    )
    response = client.post("/admin/projects/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"already added to this target" in response.data
    assert app_module.Project.query.count() == before_projects
