"""Fast Video Phase 2: automatic optimize_pair_media enqueue on PairMedia
creation, across every supported creation path.

Phase 1 built enqueue_pair_media_optimization() but deliberately left it
unwired (see its own docstring). This phase wires it in at the single
shared PairMedia-creation helper (create_pair_media_rows), called from all
three upload paths, and only AFTER each path's surrounding transaction
commits - so job identity (a real PairMedia.id) always exists, and a queue
outage can never fail project creation.

Media fixtures/login helpers are duplicated locally rather than imported
across test modules, matching the documented convention in
test_issue3e_c_multi_video_upload.py and test_issue3e_d0_resumable_multi_video.py:
a stray global site-packages `tests` package shadows dotted `tests.xxx`
imports in this environment.
"""
import os
import tempfile
from io import BytesIO

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Media + HTTP helpers (classic multipart /upload, /admin/projects/upload)
# ---------------------------------------------------------------------------
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
    from PIL import Image
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


def _patch_upload_side_effects(app_module, monkeypatch):
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
    data = {"name": name, "upload_id": f"fv2-{name}"}
    data["images"] = [(_jpeg_bytes(), f"img-{i}.jpg") for i in range(image_count)]
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


# ---------------------------------------------------------------------------
# Media + HTTP helpers (resumable session/chunk/finalize API)
# ---------------------------------------------------------------------------
def _mp4_bytes(width=64, height=64, frames=5):
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
        for _ in range(frames):
            writer.write(np.zeros((height, width, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _create_set(client, image_bytes, video_bytes, purpose="project_pair", **extra):
    payload = {
        "image_size": len(image_bytes),
        "video_size": len(video_bytes),
        "project_name": extra.pop("project_name", "Fast Video Phase 2 Story"),
        "purpose": purpose,
    }
    payload.update(extra)
    resp = client.post("/api/uploads/sessions", json=payload)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["session"]["id"]


def _create_video_only(client, video_bytes, purpose="pair_video", **extra):
    payload = {
        "image_size": 0,
        "video_size": len(video_bytes),
        "project_name": extra.pop("project_name", "Fast Video Phase 2 Story"),
        "purpose": purpose,
        "experience_type": extra.pop("experience_type", "image_video"),
    }
    payload.update(extra)
    return client.post("/api/uploads/sessions", json=payload)


def _send_chunk(client, session_id, offset, data):
    return client.post(
        f"/api/uploads/sessions/{session_id}/chunk",
        data=data,
        headers={"X-Chunk-Offset": str(offset)},
        content_type="application/octet-stream",
    )


def _upload_all(client, session_id, blob, chunk=4096):
    offset = 0
    while offset < len(blob):
        resp = _send_chunk(client, session_id, offset, blob[offset:offset + chunk])
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    return offset


def _finalize_one(client, session_id, extra_video_session_ids=None):
    body = {}
    if extra_video_session_ids:
        body["extra_video_session_ids"] = extra_video_session_ids
    return client.post(f"/api/uploads/sessions/{session_id}/finalize", json=body or None)


# ===========================================================================
# 16-19: auto-enqueue across every creation path
# ===========================================================================
def test_classic_upload_schedules_optimization_for_default_media(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("Classic FV2", 1, [(_MP4_BYTES, "v0.mp4")])

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id).one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()

    job = app_module.active_project_job(project.id, job_type="optimize_pair_media", pair_media_id=media.id)
    assert job is not None
    assert job.pair_id == pair.id


def test_resumable_single_target_schedules_optimization_for_default_media(client, app_module, db_session, login_user):
    image, video = _jpeg_bytes().read(), _mp4_bytes()
    session_id = _create_set(client, image, video)
    _upload_all(client, session_id, image + video)

    resp = _finalize_one(client, session_id)
    assert resp.status_code == 200, resp.get_json()

    session_row = app_module.UploadSession.query.get(session_id)
    project = app_module.Project.query.get(session_row.project_id)
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()

    job = app_module.active_project_job(project.id, job_type="optimize_pair_media", pair_media_id=media.id)
    assert job is not None


def test_resumable_extra_video_schedules_optimization_independently(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes().read(), _mp4_bytes(frames=5)
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 200, resp.get_json()

    project_id = app_module.UploadSession.query.get(primary_id).project_id
    pair = app_module.ProjectPair.query.filter_by(project_id=project_id).one()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert len(media_rows) == 2

    jobs = [
        app_module.active_project_job(project_id, job_type="optimize_pair_media", pair_media_id=m.id)
        for m in media_rows
    ]
    assert all(j is not None for j in jobs)
    assert jobs[0].id != jobs[1].id  # independent jobs, not one shared job


def test_two_pair_media_from_same_upload_get_independent_jobs(client, app_module, db_session, normal_user, monkeypatch):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data(
        "Two Media FV2", 1, [(_MP4_BYTES, "v0.mp4"), (_MP4_BYTES_2, "v1.mp4")], video_target_indexes=[0, 0],
    )

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="Two Media FV2").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert len(media_rows) == 2

    jobs = [
        app_module.active_project_job(project.id, job_type="optimize_pair_media", pair_media_id=m.id)
        for m in media_rows
    ]
    assert all(j is not None for j in jobs)
    assert jobs[0].id != jobs[1].id
    assert jobs[0].pair_media_id != jobs[1].pair_media_id


# ===========================================================================
# 20: no duplicate initial job for the same PairMedia
# ===========================================================================
def test_no_duplicate_initial_job_for_same_pair_media(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    media = app_module.PairMedia(pair_id=pair.id, video_filename=pair.video_filename, sort_order=0, is_default=True)
    db_session.add(media)
    db_session.commit()

    app_module._enqueue_pair_media_optimizations([media])
    app_module._enqueue_pair_media_optimizations([media])  # simulates a second, redundant call

    active = app_module.ProcessingJob.query.filter_by(
        pair_media_id=media.id, job_type="optimize_pair_media"
    ).all()
    assert len(active) == 1


# ===========================================================================
# 21: queue failure must never fail project creation
# ===========================================================================
def test_queue_failure_does_not_fail_project_creation(client, app_module, db_session, normal_user, monkeypatch):
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)

    def _boom(pair_media_id, attempt_scope="initial"):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(app_module, "enqueue_pair_media_optimization", _boom)
    data = _upload_data("Queue Boom FV2", 1, [(_MP4_BYTES, "v0.mp4")])

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302  # project creation still succeeds

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="Queue Boom FV2").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()
    # No job was created (enqueue failed), but the original is still fully
    # playable, and optimization_status remains the safe/retryable default.
    assert app_module.ProcessingJob.query.filter_by(pair_media_id=media.id).count() == 0
    assert media.optimization_status == "pending"
    assert media.video_filename == pair.video_filename


# ===========================================================================
# 22-24: queue-mode behavior (fake/inline/rq), no queue architecture changed
# ===========================================================================
def test_fake_queue_mode_creates_queued_job_without_running(client, app_module, db_session, normal_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("Fake Mode FV2", 1, [(_MP4_BYTES, "v0.mp4")])

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="Fake Mode FV2").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()
    job = app_module.ProcessingJob.query.filter_by(pair_media_id=media.id, job_type="optimize_pair_media").one()

    assert job.status == "queued"
    assert job.queue_job_id == f"fake-{job.id}"
    db_session.expire_all()
    media = app_module.PairMedia.query.get(media.id)
    assert media.optimization_status == "pending"  # fake mode never runs the job


def test_inline_queue_mode_runs_job_synchronously(client, app_module, db_session, normal_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "inline")
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("Inline Mode FV2", 1, [(_MP4_BYTES, "v0.mp4")])

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="Inline Mode FV2").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()
    job = app_module.ProcessingJob.query.filter_by(pair_media_id=media.id, job_type="optimize_pair_media").one()

    # Inline mode runs the job in-request (processing_queue._enqueue_transport
    # calls run_processing_job before returning) - by the time the upload
    # response comes back the job has already left its initial "queued"
    # state, whatever ffmpeg availability in this environment lets it reach
    # ("completed"/"failed" if ffmpeg is resolvable, "retrying" if the
    # BINARY_UNAVAILABLE retryable-failure path fires instead).
    assert job.status in {"completed", "failed", "retrying"}
    assert job.status != "queued"
    # The original is untouched either way - Phase 1's own safety guarantee.
    assert os.path.exists(os.path.join(app_module.VIDEOS_DIR, media.video_filename))


def test_rq_queue_mode_calls_transport_for_each_pair_media(client, app_module, db_session, normal_user, monkeypatch):
    import processing_queue

    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    calls = []

    def spy_transport(job):
        calls.append((job.job_type, job.pair_media_id))
        return f"rq-sentinel-{job.id}"

    monkeypatch.setattr(processing_queue, "_enqueue_transport", spy_transport)
    _patch_upload_side_effects(app_module, monkeypatch)
    _login_user(client, normal_user)
    data = _upload_data("RQ Mode FV2", 1, [(_MP4_BYTES, "v0.mp4")])

    resp = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302

    project = app_module.Project.query.filter_by(owner_user_id=normal_user.id, name="RQ Mode FV2").one()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    media = app_module.PairMedia.query.filter_by(pair_id=pair.id).one()

    optimize_calls = [c for c in calls if c[0] == "optimize_pair_media"]
    assert ("optimize_pair_media", media.id) in optimize_calls
    job = app_module.ProcessingJob.query.filter_by(pair_media_id=media.id, job_type="optimize_pair_media").one()
    assert job.queue_job_id == f"rq-sentinel-{job.id}"
