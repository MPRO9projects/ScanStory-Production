"""Tests for the V1 Wave 5 resumable-upload backend (app.py's
/api/uploads/sessions routes + models.UploadSession + the
cleanup-upload-sessions CLI).

Reuses the existing _jpeg_bytes()/_mp4_bytes() fixtures (same real,
decodable media the non-resumable upload tests already rely on) so
validate_image()/validate_video() are exercised for real, never a second
weaker validator. SCANSTORY_TESTING=1 (set by isolated_app) already puts
processing_queue._queue_mode() into "fake" mode, so enqueue never touches
real Redis/RQ without any extra mocking here.
"""
import os
import json
import tempfile
from datetime import timedelta
from io import BytesIO
from unittest.mock import MagicMock

import cv2
import numpy as np
from PIL import Image
from werkzeug.security import generate_password_hash

# Deliberately NOT `from tests.security.test_upload_validation import
# _jpeg_bytes, _mp4_bytes`: this environment has a stray global
# site-packages `tests` package (D:\python\lib\site-packages\tests)
# shadowing the local `tests/` directory for any dotted `tests.xxx`
# import, which breaks that cross-module import identically for
# tests/integration/test_upload_edge_hardening.py - a pre-existing file,
# confirmed to fail the same way on the unmodified base commit (see the
# delivery report). Duplicating these two small fixtures locally sidesteps
# that pre-existing environment issue entirely rather than depending on it.


def _jpeg_bytes(width=640, height=480):
    out = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(out, format="JPEG")
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


def _patch_qr(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: None)


def _create_session(client, image_size, video_size, **extra):
    payload = {"image_size": image_size, "video_size": video_size}
    payload.update(extra)
    return client.post("/api/uploads/sessions", json=payload)


def _send_chunk(client, session_id, offset, data):
    return client.post(
        f"/api/uploads/sessions/{session_id}/chunk",
        data=data,
        headers={"X-Chunk-Offset": str(offset)},
        content_type="application/octet-stream",
    )


def _create_and_upload(client, image_bytes=None, video_bytes=None, chunk_size=None, **extra):
    image_bytes = _jpeg_bytes() if image_bytes is None else image_bytes
    video_bytes = _mp4_bytes() if video_bytes is None else video_bytes
    resp = _create_session(client, len(image_bytes), len(video_bytes), **extra)
    assert resp.status_code == 201, resp.get_json()
    session_id = resp.get_json()["session"]["id"]
    combined = image_bytes + video_bytes
    step = chunk_size or len(combined)
    offset = 0
    while offset < len(combined):
        chunk = combined[offset:offset + step]
        r = _send_chunk(client, session_id, offset, chunk)
        assert r.status_code == 200, r.get_json()
        offset += len(chunk)
    return session_id


def _finalize(client, session_id):
    return client.post(f"/api/uploads/sessions/{session_id}/finalize")


def _cancel(client, session_id):
    return client.post(f"/api/uploads/sessions/{session_id}/cancel")


def _status(client, session_id):
    return client.get(f"/api/uploads/sessions/{session_id}")


def _structured_records(caplog, attr):
    return [getattr(record, attr) for record in caplog.records if getattr(record, attr, None)]


def _assert_safe_timing_payload(payload):
    raw = json.dumps(payload, sort_keys=True)
    assert "@" not in raw
    assert "password" not in raw.lower()
    assert "secret" not in raw.lower()
    assert "token" not in raw.lower()
    assert "not uploaded content" not in raw
    assert "F:\\" not in raw


def _assert_non_negative_timing_fields(payload):
    for key, value in payload.items():
        if key.endswith("_ms"):
            assert isinstance(value, (int, float))
            assert value >= 0


# ---------------------------------------------------------------------
# 1. Create session
# ---------------------------------------------------------------------
def test_create_session_success(client, login_user):
    resp = _create_session(client, 1000, 2000, project_name="My Project")
    assert resp.status_code == 201
    session = resp.get_json()["session"]
    assert session["status"] == "active"
    assert session["current_offset"] == 0
    assert session["expected_total_size"] == 3000
    assert session["project_id"] is None
    assert session["pair_id"] is None


def test_create_session_requires_auth(client):
    resp = _create_session(client, 1000, 2000)
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "UNAUTHENTICATED"


def test_create_session_rejects_oversized_image(client, app_module, login_user):
    resp = _create_session(client, app_module.MAX_IMAGE_SIZE + 1, 2000)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "IMAGE_TOO_LARGE"
    assert app_module.UploadSession.query.count() == 0


def test_create_session_rejects_oversized_video(client, app_module, login_user):
    resp = _create_session(client, 1000, app_module.MAX_VIDEO_SIZE + 1)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "VIDEO_TOO_LARGE"
    assert app_module.UploadSession.query.count() == 0


def test_create_session_rejects_non_positive_sizes(client, login_user):
    resp = _create_session(client, 0, 100)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_SIZE"


# ---------------------------------------------------------------------
# 2. Chunk upload: sequential assembly, offset rules, idempotent retry
# ---------------------------------------------------------------------
def test_sequential_chunks_assemble_correctly(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    image_bytes, video_bytes = _jpeg_bytes(), _mp4_bytes()
    session_id = _create_and_upload(client, image_bytes, video_bytes, chunk_size=37)

    status = _status(client, session_id).get_json()["session"]
    assert status["current_offset"] == len(image_bytes) + len(video_bytes)
    assert status["status"] == "active"

    resp = _finalize(client, session_id)
    assert resp.status_code == 200, resp.get_json()
    pair = app_module.ProjectPair.query.first()
    assert pair is not None
    assert pair.image_size == len(image_bytes)
    assert pair.video_size == len(video_bytes)


def test_wrong_offset_rejected(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    bad = _send_chunk(client, session_id, 5, b"x" * 5)
    assert bad.status_code == 409
    assert bad.get_json()["code"] == "OFFSET_MISMATCH"


def test_duplicate_chunk_retry_is_idempotent(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    first = _send_chunk(client, session_id, 0, b"a" * 6)
    assert first.status_code == 200
    assert first.get_json()["current_offset"] == 6

    retry = _send_chunk(client, session_id, 0, b"a" * 6)
    assert retry.status_code == 200
    assert retry.get_json()["current_offset"] == 6
    assert retry.get_json()["note"] == "duplicate_chunk_ignored"


def test_upload_session_emits_structured_create_chunk_and_finalize_timings(
    client, app_module, login_user, monkeypatch, caplog
):
    _patch_qr(app_module, monkeypatch)
    with caplog.at_level("INFO"):
        session_id = _create_and_upload(client)
        final = _finalize(client, session_id)

    assert final.status_code == 200
    records = _structured_records(caplog, "upload_timing")
    by_event = {payload["event"]: payload for payload in records}
    assert {"upload_session_create", "upload_session_chunk", "upload_session_finalize"} <= set(by_event)

    created = by_event["upload_session_create"]
    assert created["upload_session_id"] == session_id
    assert created["owner_type"] == "user"
    assert created["pair_count"] == 1
    assert created["total_bytes"] == created["image_bytes"] + created["video_bytes"]
    assert created["status"] == "active"

    chunk = by_event["upload_session_chunk"]
    assert chunk["upload_session_id"] == session_id
    assert chunk["duplicate_chunk"] is False
    assert chunk["offset_mismatch"] is False
    assert chunk["status"] == "accepted"

    finalized = by_event["upload_session_finalize"]
    assert finalized["upload_session_id"] == session_id
    assert finalized["project_id"] == final.get_json()["session"]["project_id"]
    assert finalized["pair_count"] == 1
    assert finalized["status"] == "completed"
    assert finalized["recovered_existing_completion"] is False

    for payload in records:
        _assert_non_negative_timing_fields(payload)
        _assert_safe_timing_payload(payload)


def test_chunk_duplicate_and_offset_mismatch_timings_are_structured(client, login_user, caplog):
    with caplog.at_level("INFO"):
        resp = _create_session(client, 10, 10)
        session_id = resp.get_json()["session"]["id"]
        first = _send_chunk(client, session_id, 0, b"a" * 6)
        duplicate = _send_chunk(client, session_id, 0, b"a" * 6)
        mismatch = _send_chunk(client, session_id, 12, b"b" * 2)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert mismatch.status_code == 409
    chunks = [p for p in _structured_records(caplog, "upload_timing") if p["event"] == "upload_session_chunk"]
    assert any(p["duplicate_chunk"] is True and p["status"] == "duplicate" for p in chunks)
    assert any(p["offset_mismatch"] is True and p["safe_error_code"] == "OFFSET_MISMATCH" for p in chunks)
    for payload in chunks:
        _assert_non_negative_timing_fields(payload)
        _assert_safe_timing_payload(payload)


def test_empty_chunk_rejected(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    bad = _send_chunk(client, session_id, 0, b"")
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "EMPTY_CHUNK"


def test_configured_max_size_chunk_succeeds(client, app_module, login_user):
    max_chunk = app_module.app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"]
    resp = _create_session(client, max_chunk, max_chunk)
    session_id = resp.get_json()["session"]["id"]
    ok = _send_chunk(client, session_id, 0, b"x" * max_chunk)
    assert ok.status_code == 200
    assert ok.get_json()["current_offset"] == max_chunk


def test_oversized_chunk_rejected_without_advancing_offset(client, app_module, login_user):
    max_chunk = app_module.app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"]
    resp = _create_session(client, max_chunk + 1, max_chunk + 1)
    session_id = resp.get_json()["session"]["id"]
    bad = _send_chunk(client, session_id, 0, b"x" * (max_chunk + 1))
    assert bad.status_code == 413
    assert bad.get_json()["code"] == "CHUNK_TOO_LARGE"
    status = _status(client, session_id).get_json()["session"]
    assert status["current_offset"] == 0


def test_chunk_exceeding_declared_size_rejected(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    bad = _send_chunk(client, session_id, 0, b"x" * 25)
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "CHUNK_EXCEEDS_EXPECTED_SIZE"


def test_missing_offset_header_rejected(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    bad = client.post(
        f"/api/uploads/sessions/{session_id}/chunk", data=b"x" * 5, content_type="application/octet-stream"
    )
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "INVALID_OFFSET"


# ---------------------------------------------------------------------
# Ownership enforcement
# ---------------------------------------------------------------------
def test_ownership_enforced_across_users(client, app_module, db_session, normal_user, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]

    other = app_module.User(
        email="other-upload@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
    )
    db_session.add(other)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = other.id

    assert _status(client, session_id).status_code == 404
    assert _send_chunk(client, session_id, 0, b"x").status_code == 404
    assert _finalize(client, session_id).status_code == 404
    assert _cancel(client, session_id).status_code == 404


def test_admin_cannot_access_users_session(client, app_module, login_user, admin):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]

    with client.session_transaction() as sess:
        sess.pop("user_id", None)
        sess["admin_id"] = admin.id

    assert _status(client, session_id).status_code == 404


# ---------------------------------------------------------------------
# Session-state rejections
# ---------------------------------------------------------------------
def test_expired_session_rejected(client, app_module, db_session, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    row = app_module.UploadSession.query.get(session_id)
    row.expires_at = app_module.get_utc_now() - timedelta(minutes=1)
    db_session.commit()

    bad = _send_chunk(client, session_id, 0, b"x" * 5)
    assert bad.status_code == 409
    assert bad.get_json()["code"] == "SESSION_EXPIRED"
    assert app_module.UploadSession.query.get(session_id).status == "expired"


def test_cancelled_session_rejects_chunk_and_finalize(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    assert _cancel(client, session_id).status_code == 200

    bad_chunk = _send_chunk(client, session_id, 0, b"x" * 5)
    assert bad_chunk.status_code == 409
    assert bad_chunk.get_json()["code"] == "SESSION_CANCELLED"

    bad_finalize = _finalize(client, session_id)
    assert bad_finalize.status_code == 409


def test_cancel_only_valid_from_active(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    assert _cancel(client, session_id).status_code == 200
    again = _cancel(client, session_id)
    assert again.status_code == 409


# ---------------------------------------------------------------------
# Finalize: exactly once, incomplete rejection
# ---------------------------------------------------------------------
def test_finalize_incomplete_upload_rejected(client, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    _send_chunk(client, session_id, 0, b"x" * 5)  # only half of the declared size
    bad = _finalize(client, session_id)
    assert bad.status_code == 409
    assert bad.get_json()["code"] == "INCOMPLETE_UPLOAD"


def test_finalize_twice_rejected_not_double_processed(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)
    first = _finalize(client, session_id)
    assert first.status_code == 200

    second = _finalize(client, session_id)
    assert second.status_code == 409
    assert second.get_json()["code"] == "ALREADY_FINALIZED"
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 1


def test_lost_finalize_success_recovered_from_authoritative_status(
    client, app_module, normal_user, login_user, monkeypatch, caplog
):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)

    with caplog.at_level("INFO"):
        first = _finalize(client, session_id)
        assert first.status_code == 200
        project_id = first.get_json()["session"]["project_id"]

        retry = _finalize(client, session_id)
    assert retry.status_code == 409
    assert retry.get_json()["code"] == "ALREADY_FINALIZED"

    status = _status(client, session_id)
    assert status.status_code == 200
    session = status.get_json()["session"]
    assert session["status"] == "completed"
    assert session["project_id"] == project_id

    assert app_module.UploadSession.query.count() == 1
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 1
    assert app_module.ProcessingJob.query.filter_by(project_id=project_id).count() == 1
    app_module.db.session.refresh(normal_user)
    assert (normal_user.projects_used or 0) == 1
    finalize_timings = [
        payload for payload in _structured_records(caplog, "upload_timing")
        if payload["event"] == "upload_session_finalize"
    ]
    assert any(payload.get("recovered_existing_completion") is True for payload in finalize_timings)
    for payload in finalize_timings:
        _assert_non_negative_timing_fields(payload)
        _assert_safe_timing_payload(payload)


def test_foreign_user_cannot_recover_completed_upload_session(
    client, app_module, db_session, login_user, monkeypatch
):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)
    assert _finalize(client, session_id).status_code == 200

    other = app_module.User(
        email="foreign-recovery@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
    )
    db_session.add(other)
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = other.id

    assert _status(client, session_id).status_code == 404
    assert _finalize(client, session_id).status_code == 404
    assert app_module.UploadSession.query.count() == 1
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 1


# ---------------------------------------------------------------------
# Validation failure at finalize: cleanup, no phantom Project/Pair, no quota
# ---------------------------------------------------------------------
def test_finalize_invalid_image_cleans_up_and_consumes_no_quota(client, app_module, normal_user, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    garbage_image = b"not a real image at all, just junk bytes"
    session_id = _create_and_upload(client, image_bytes=garbage_image)

    resp = _finalize(client, session_id)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "IMAGE_VALIDATION_FAILED"

    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0
    app_module.db.session.refresh(normal_user)
    assert (normal_user.projects_used or 0) == 0

    row = app_module.UploadSession.query.get(session_id)
    assert row.status == "failed"
    assert row.failure_code == "IMAGE_VALIDATION_FAILED"
    temp_path = app_module._upload_session_temp_path(row.storage_token)
    assert not app_module.os.path.exists(temp_path)


def test_finalize_invalid_video_cleans_up(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client, video_bytes=b"definitely not an mp4")

    resp = _finalize(client, session_id)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "VIDEO_VALIDATION_FAILED"
    assert app_module.Project.query.count() == 0


# ---------------------------------------------------------------------
# Enqueue exactly once / queue failure recovery
# ---------------------------------------------------------------------
def test_enqueue_called_exactly_once_on_success(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    mock_enqueue = MagicMock(wraps=app_module.enqueue_project_pair_processing)
    monkeypatch.setattr(app_module, "enqueue_project_pair_processing", mock_enqueue)

    session_id = _create_and_upload(client)
    resp = _finalize(client, session_id)
    assert resp.status_code == 200
    assert mock_enqueue.call_count == 1

    # A second finalize attempt must not enqueue again.
    _finalize(client, session_id)
    assert mock_enqueue.call_count == 1


def test_finalize_enqueue_failure_leaves_assembled_state(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)

    def _boom(project_id):
        raise app_module.QueueUnavailable("simulated queue outage")

    monkeypatch.setattr(app_module, "enqueue_project_pair_processing", _boom)

    session_id = _create_and_upload(client)
    resp = _finalize(client, session_id)
    assert resp.status_code == 502
    assert resp.get_json()["code"] == "QUEUE_ENQUEUE_FAILED"

    row = app_module.UploadSession.query.get(session_id)
    assert row.status == "assembled"
    assert row.project_id is not None
    assert row.pair_id is not None
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 1

    status_resp = _status(client, session_id)
    assert status_resp.get_json()["session"]["status"] == "assembled"


def test_finalize_recovers_after_enqueue_failure_via_retry(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    calls = {"n": 0}
    real_enqueue = app_module.enqueue_project_pair_processing

    def _flaky(project_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise app_module.QueueUnavailable("simulated queue outage")
        return real_enqueue(project_id)

    monkeypatch.setattr(app_module, "enqueue_project_pair_processing", _flaky)

    session_id = _create_and_upload(client)
    first = _finalize(client, session_id)
    assert first.status_code == 502

    second = _finalize(client, session_id)
    assert second.status_code == 200
    assert second.get_json()["session"]["status"] == "completed"
    # Recovery must not create a second Project/Pair or re-consume quota.
    assert app_module.Project.query.count() == 1
    assert app_module.ProjectPair.query.count() == 1


# ---------------------------------------------------------------------
# Quota invariants
# ---------------------------------------------------------------------
def test_quota_not_consumed_until_finalize_succeeds(client, app_module, normal_user, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)
    app_module.db.session.refresh(normal_user)
    assert (normal_user.projects_used or 0) == 0  # created + uploaded, not finalized yet

    resp = _finalize(client, session_id)
    assert resp.status_code == 200
    app_module.db.session.refresh(normal_user)
    assert (normal_user.projects_used or 0) == 1


def test_admin_owned_session_never_consumes_quota(client, app_module, login_admin, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)
    resp = _finalize(client, session_id)
    assert resp.status_code == 200
    project = app_module.Project.query.first()
    assert project.owner_admin_id is not None
    assert project.owner_user_id is None


# ---------------------------------------------------------------------
# Cleanup CLI
# ---------------------------------------------------------------------
def test_cleanup_cli_dry_run_changes_nothing(client, app_module, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    row = app_module.UploadSession.query.get(session_id)
    row.expires_at = app_module.get_utc_now() - timedelta(minutes=1)
    app_module.db.session.commit()

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions"])
    assert result.exit_code == 0
    assert "Mode: dry-run" in result.output
    assert app_module.UploadSession.query.get(session_id).status == "active"


def test_cleanup_cli_apply_expires_and_deletes_temp_file(client, app_module, login_user):
    resp = _create_session(client, 10, 10)
    session_id = resp.get_json()["session"]["id"]
    row = app_module.UploadSession.query.get(session_id)
    temp_path = app_module._upload_session_temp_path(row.storage_token)
    assert app_module.os.path.exists(temp_path)
    row.expires_at = app_module.get_utc_now() - timedelta(minutes=1)
    app_module.db.session.commit()

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions", "--apply"])
    assert result.exit_code == 0
    assert "Expired: 1" in result.output
    refreshed = app_module.UploadSession.query.get(session_id)
    assert refreshed.status == "expired"
    assert refreshed.failure_code == "SESSION_TTL_EXPIRED"
    assert not app_module.os.path.exists(temp_path)


def test_cleanup_cli_never_touches_completed_sessions(client, app_module, login_user, monkeypatch):
    _patch_qr(app_module, monkeypatch)
    session_id = _create_and_upload(client)
    _finalize(client, session_id)
    row = app_module.UploadSession.query.get(session_id)
    assert row.status == "completed"
    # Force it to look "stale" by every timestamp-based signal the CLI checks.
    row.expires_at = app_module.get_utc_now() - timedelta(days=30)
    row.updated_at = app_module.get_utc_now() - timedelta(days=30)
    app_module.db.session.commit()

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions", "--apply"])
    assert result.exit_code == 0
    assert "Candidates found (bounded to limit=200): 0" in result.output
    assert app_module.UploadSession.query.get(session_id).status == "completed"


def test_cleanup_cli_bounded_batch_limit(client, app_module, login_user):
    ids = []
    for _ in range(5):
        resp = _create_session(client, 10, 10)
        sid = resp.get_json()["session"]["id"]
        row = app_module.UploadSession.query.get(sid)
        row.expires_at = app_module.get_utc_now() - timedelta(minutes=1)
        ids.append(sid)
    app_module.db.session.commit()

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["cleanup-upload-sessions", "--apply", "--limit", "2"])
    assert result.exit_code == 0
    assert "Candidates found (bounded to limit=2): 2" in result.output
    assert "Expired: 2" in result.output
    expired_count = sum(1 for sid in ids if app_module.UploadSession.query.get(sid).status == "expired")
    assert expired_count == 2
