"""Issue 3E-D0: resumable multi-video transport foundation.

The live Creator submits through the resumable-upload API
(submitResumableSinglePair/submitResumableMultiPair), not the classic
multipart /upload route - so the Issue 3E-C "one target -> N PairMedia"
backend was unreachable from real browsers until this phase. This file
proves the SAME contract now works end-to-end through
/api/uploads/sessions -> /api/uploads/sessions/<id>/chunk ->
/api/uploads/sessions/<id>/finalize (and the project-group finalize route),
using ONE resumable session per physical video file (a new 'pair_video'
purpose, image_size=0 - the same shape Direct QR video-only sessions
already use) rather than growing one session's byte stream to carry N
videos, so each video keeps independent resumability.

Media fixtures/helpers are duplicated from
tests/integration/test_multi_pair_resumable_upload.py for the same
documented reason that file gives: a stray global site-packages `tests`
package shadows dotted `tests.xxx` imports in this environment.
"""
import os
import tempfile

import cv2
import numpy as np
import pytest


def _jpeg_bytes(width=640, height=480, shade=120):
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    Image.new("RGB", (width, height), (shade, 80, 40)).save(out, format="JPEG")
    return out.getvalue()


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
        "project_name": extra.pop("project_name", "3E-D0 Story"),
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
        "project_name": extra.pop("project_name", "3E-D0 Story"),
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


def _upload_partial(client, session_id, blob, upto, chunk=4096):
    upto = max(1, min(upto, len(blob) - 1))
    offset = 0
    while offset < upto:
        end = min(offset + chunk, upto)
        resp = _send_chunk(client, session_id, offset, blob[offset:end])
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    return offset


def _finalize_one(client, session_id, extra_video_session_ids=None):
    body = {}
    if extra_video_session_ids:
        body["extra_video_session_ids"] = extra_video_session_ids
    return client.post(f"/api/uploads/sessions/{session_id}/finalize", json=body or None)


def _finalize_project(client, session_ids, extra_video_session_ids=None):
    body = {"session_ids": list(session_ids)}
    if extra_video_session_ids:
        body["extra_video_session_ids"] = extra_video_session_ids
    return client.post("/api/uploads/projects/finalize", json=body)


def _status(client, session_id):
    return client.get(f"/api/uploads/sessions/{session_id}")


def _enable_multi_video(db_session, plan, max_videos=5):
    plan.allow_multi_video_per_target = True
    plan.max_videos_per_target = max_videos
    db_session.commit()


def _login_user(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user.id


# ===========================================================================
# 1-2: legacy contract unchanged.
# ===========================================================================
def test_legacy_one_target_one_video_resumable_still_works(client, app_module, db_session, login_user):
    image, video = _jpeg_bytes(), _mp4_bytes()
    session_id = _create_set(client, image, video)
    _upload_all(client, session_id, image + video)

    resp = _finalize_one(client, session_id)
    assert resp.status_code == 200, resp.get_json()

    session_row = app_module.UploadSession.query.get(session_id)
    project = app_module.Project.query.get(session_row.project_id)
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).one()
    assert pair.video_filename == f"{project.id}_0.mp4"
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1


def test_legacy_multi_target_resumable_still_works(client, app_module, db_session, login_user):
    image_a, video_a = _jpeg_bytes(shade=100), _mp4_bytes(frames=5)
    image_b, video_b = _jpeg_bytes(shade=150), _mp4_bytes(frames=6)
    set_a = _create_set(client, image_a, video_a, purpose="project_content_set")
    set_b = _create_set(client, image_b, video_b, purpose="project_content_set")
    _upload_all(client, set_a, image_a + video_a)
    _upload_all(client, set_b, image_b + video_b)

    resp = _finalize_project(client, [set_a, set_b])
    assert resp.status_code == 200, resp.get_json()

    project_id = app_module.UploadSession.query.get(set_a).project_id
    pairs = app_module.ProjectPair.query.filter_by(project_id=project_id).order_by(app_module.ProjectPair.pair_index).all()
    assert len(pairs) == 2
    for pair in pairs:
        assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1


# ===========================================================================
# 3-7, 9(entitlement path): one target + multiple resumable videos.
# ===========================================================================
def test_one_target_two_videos_finalizes_to_one_project_pair(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes(frames=5)
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 200, resp.get_json()

    project_id = app_module.UploadSession.query.get(primary_id).project_id
    pairs = app_module.ProjectPair.query.filter_by(project_id=project_id).all()
    assert len(pairs) == 1
    pair = pairs[0]
    media_rows = app_module.PairMedia.query.filter_by(pair_id=pair.id).order_by(app_module.PairMedia.sort_order).all()
    assert len(media_rows) == 2
    assert [m.sort_order for m in media_rows] == [0, 1]
    assert sum(1 for m in media_rows if m.is_default) == 1
    assert pair.video_filename == media_rows[0].video_filename
    assert pair.video_size == media_rows[0].video_size


def test_one_target_three_videos_creates_three_pair_media_rows(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes(frames=5)
    video2, video3 = _mp4_bytes(frames=6), _mp4_bytes(frames=7)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_ids = []
    for v in (video2, video3):
        eid = _create_video_only(client, v).get_json()["session"]["id"]
        _upload_all(client, eid, v)
        extra_ids.append(eid)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=extra_ids)
    assert resp.status_code == 200, resp.get_json()

    project_id = app_module.UploadSession.query.get(primary_id).project_id
    pair = app_module.ProjectPair.query.filter_by(project_id=project_id).one()
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 3


# ===========================================================================
# 8-9: entitlement enforcement.
# ===========================================================================
def test_entitlement_disabled_rejects_extra_video(client, app_module, db_session, login_user, normal_user):
    assert normal_user.subscription_plan.allow_multi_video_per_target is False
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "MULTI_VIDEO_NOT_ENTITLED"

    # Nothing lost: both sessions are still resumable/finalizable.
    assert app_module.UploadSession.query.get(primary_id).status == "active"
    assert app_module.UploadSession.query.get(extra_id).status == "active"
    assert app_module.Project.query.count() == 0


def test_max_limit_rejects_correctly(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=1)
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "VIDEO_LIMIT_REACHED"
    assert "1" in resp.get_json()["error"]


# ===========================================================================
# 10-13: session resolution guards.
# ===========================================================================
def test_session_ownership_mismatch_rejected(client, app_module, db_session, login_user, normal_user, admin):
    """A different account entirely (an Admin, which the resumable API also
    supports as an owner - see _upload_identity) uploads its own additional
    video; the normal_user creator must not be able to attach a video it
    does not own to its own target."""
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)

    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)

    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id
    extra_resp = _create_video_only(client, video2)
    extra_id = extra_resp.get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    _login_user(client, normal_user)
    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "NOT_FOUND"


def test_incomplete_video_session_rejected(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=8)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_partial(client, extra_id, video2, upto=len(video2) // 2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "SESSION_INCOMPLETE"
    # Primary is untouched by the rejection, still resumable.
    assert app_module.UploadSession.query.get(primary_id).status == "active"


def test_image_video_session_used_as_extra_video_rejected(client, app_module, db_session, login_user, normal_user):
    """Item 12: an ordinary image+video session (has its own recognition
    image) may not be reused as someone's additional video."""
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    image2, video2 = _jpeg_bytes(shade=200), _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    other_pair_session = _create_set(client, image2, video2)
    _upload_all(client, other_pair_session, image2 + video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[other_pair_session])
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_PURPOSE"


def test_pair_video_session_used_as_primary_rejected(client, app_module, db_session, login_user, normal_user):
    """Item 13: an additional-video (pure video, no image) session cannot be
    finalized on its own as if it were a target."""
    video = _mp4_bytes()
    session_id = _create_video_only(client, video).get_json()["session"]["id"]
    _upload_all(client, session_id, video)

    resp = _finalize_one(client, session_id)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "INVALID_PURPOSE"


# ===========================================================================
# 14-15: resume/retry independence.
# ===========================================================================
def test_interrupted_extra_video_resumes_independently(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=10)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)  # primary fully confirmed
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    partial_offset = _upload_partial(client, extra_id, video2, upto=len(video2) // 2)

    # Sibling's confirmed bytes are untouched by the partial extra upload.
    assert app_module.UploadSession.query.get(primary_id).current_offset == len(image) + len(video1)

    # Resume the extra video from its OWN confirmed offset - no re-send of
    # the image or video1's bytes required.
    offset = partial_offset
    while offset < len(video2):
        resp = _send_chunk(client, extra_id, offset, video2[offset:offset + 4096])
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    assert offset == len(video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 200, resp.get_json()


def test_completed_sibling_sessions_are_not_re_uploaded(client, app_module, db_session, login_user, normal_user):
    """Item 15: uploading bytes for one video session never advances a
    sibling's offset - each physical file's resumability is independent."""
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    primary_offset_before = app_module.UploadSession.query.get(primary_id).current_offset

    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_partial(client, extra_id, video2, upto=len(video2) // 2)

    assert app_module.UploadSession.query.get(primary_id).current_offset == primary_offset_before


# ===========================================================================
# 16: storage accounting once per physical file.
# ===========================================================================
def test_storage_accounted_once_per_physical_uploaded_video(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    image, video1 = _jpeg_bytes(), _mp4_bytes(frames=5)
    video2, video3 = _mp4_bytes(frames=6), _mp4_bytes(frames=7)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_ids = []
    for v in (video2, video3):
        eid = _create_video_only(client, v).get_json()["session"]["id"]
        _upload_all(client, eid, v)
        extra_ids.append(eid)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=extra_ids)
    assert resp.status_code == 200, resp.get_json()

    project_id = app_module.UploadSession.query.get(primary_id).project_id
    video_media_objects = app_module.MediaObject.query.filter_by(
        project_id=project_id, media_role=app_module._storage.MEDIA_ROLE_VIDEO,
    ).all()
    assert len(video_media_objects) == 3
    pair = app_module.ProjectPair.query.filter_by(project_id=project_id).one()
    total_on_disk = sum(m.video_size for m in app_module.PairMedia.query.filter_by(pair_id=pair.id).all())
    total_billed = sum(mo.size_bytes for mo in video_media_objects)
    assert total_billed == total_on_disk


# ===========================================================================
# 17: finalize failure leaves no partial rows.
# ===========================================================================
def test_finalize_failure_leaves_no_partial_rows(client, app_module, db_session, login_user, normal_user):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=1)
    before_projects = app_module.Project.query.count()
    before_pairs = app_module.ProjectPair.query.count()
    before_media = app_module.PairMedia.query.count()

    image, video1 = _jpeg_bytes(), _mp4_bytes()
    video2 = _mp4_bytes(frames=6)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_id = _create_video_only(client, video2).get_json()["session"]["id"]
    _upload_all(client, extra_id, video2)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=[extra_id])
    assert resp.status_code == 403

    assert app_module.Project.query.count() == before_projects
    assert app_module.ProjectPair.query.count() == before_pairs
    assert app_module.PairMedia.query.count() == before_media


# ===========================================================================
# 18: recognition processing scheduled once per target, not per video.
# ===========================================================================
def test_recognition_scheduled_once_per_target(client, app_module, db_session, login_user, normal_user, monkeypatch):
    _enable_multi_video(db_session, normal_user.subscription_plan, max_videos=5)
    calls = []
    real_schedule = app_module._schedule_project_pair_processing

    def _counting_schedule(project_id, *a, **k):
        calls.append(project_id)
        return real_schedule(project_id, *a, **k)

    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", _counting_schedule)

    image, video1 = _jpeg_bytes(), _mp4_bytes(frames=5)
    video2, video3 = _mp4_bytes(frames=6), _mp4_bytes(frames=7)
    primary_id = _create_set(client, image, video1)
    _upload_all(client, primary_id, image + video1)
    extra_ids = []
    for v in (video2, video3):
        eid = _create_video_only(client, v).get_json()["session"]["id"]
        _upload_all(client, eid, v)
        extra_ids.append(eid)

    resp = _finalize_one(client, primary_id, extra_video_session_ids=extra_ids)
    assert resp.status_code == 200, resp.get_json()
    assert len(calls) == 1


# ===========================================================================
# 20: Direct QR unaffected - a video-only 'pair_video' session is rejected
# outright for direct_qr, so Direct QR can never accidentally grow extra
# videos through this new purpose.
# ===========================================================================
def test_direct_qr_cannot_use_pair_video_purpose(client, app_module, db_session, login_user):
    video = _mp4_bytes()
    resp = _create_video_only(client, video, experience_type="direct_qr")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_PURPOSE"


def test_direct_qr_legacy_resumable_still_works(client, app_module, db_session, login_user):
    video = _mp4_bytes()
    session_id = _create_set(
        client, b"", video, purpose="project_pair", experience_type="direct_qr", playback_mode="direct",
    )
    _upload_all(client, session_id, video)
    resp = _finalize_one(client, session_id)
    assert resp.status_code == 200, resp.get_json()
    project_id = app_module.UploadSession.query.get(session_id).project_id
    project = app_module.Project.query.get(project_id)
    assert project.experience_type == "direct_qr"
    pair = app_module.ProjectPair.query.filter_by(project_id=project_id).one()
    assert app_module.PairMedia.query.filter_by(pair_id=pair.id).count() == 1
