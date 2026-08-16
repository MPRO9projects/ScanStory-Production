from datetime import timedelta
import json
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash


def _make_project_pair(app_module, db_session, owner_user=None, owner_admin=None, tmp_image=True):
    project = app_module.Project(
        name="Queued Project",
        owner_user_id=owner_user.id if owner_user else None,
        owner_admin_id=owner_admin.id if owner_admin else None,
    )
    db_session.add(project)
    db_session.commit()
    image_dir = Path(app_module.ADMIN_IMAGES_DIR if owner_admin else app_module.IMAGES_DIR)
    video_dir = Path(app_module.ADMIN_VIDEOS_DIR if owner_admin else app_module.VIDEOS_DIR)
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    if tmp_image:
        Image.new("RGB", (64, 64), (120, 80, 40)).save(image_dir / f"{project.id}_0.jpg", format="JPEG")
    (video_dir / f"{project.id}_0.mp4").write_bytes(b"video")
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=f"{project.id}_0.jpg",
        video_filename=f"{project.id}_0.mp4",
        image_path=f"/image/{project.id}/0",
        is_processed=False,
        processing_status="uploaded",
        feature_extraction_status="pending",
    )
    db_session.add(pair)
    db_session.commit()
    return project, pair


def _structured_records(caplog, attr):
    return [getattr(record, attr) for record in caplog.records if getattr(record, attr, None)]


def _assert_timing_payload_is_safe(payload):
    raw = json.dumps(payload, sort_keys=True)
    assert "@" not in raw
    assert "password" not in raw.lower()
    assert "secret" not in raw.lower()
    assert "token" not in raw.lower()
    assert "F:\\" not in raw
    for key, value in payload.items():
        if key.endswith("_ms"):
            assert isinstance(value, (int, float))
            assert value >= 0


def test_enqueue_creates_queued_record_and_fake_queue_id(app_module, db_session, normal_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)

    job, created = app_module.enqueue_project_pair_processing(project.id)

    assert created is True
    assert job.status == "queued"
    assert job.queue_job_id == f"fake-{job.id}"
    assert job.project_id == project.id
    assert job.owner_user_id == normal_user.id


def test_duplicate_enqueue_returns_active_job(app_module, db_session, normal_user):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)

    first, first_created = app_module.enqueue_project_pair_processing(project.id)
    second, second_created = app_module.enqueue_project_pair_processing(project.id)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_enqueue_failure_records_safe_failed_state(app_module, db_session, normal_user, monkeypatch):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    monkeypatch.setenv("SCANSTORY_QUEUE_REQUIRED", "1")
    monkeypatch.delenv("REDIS_URL", raising=False)

    job = app_module._schedule_project_pair_processing(project.id)

    assert job is None
    failed = app_module.ProcessingJob.query.filter_by(project_id=project.id).first()
    assert failed is not None
    assert failed.status == "failed"
    assert failed.safe_error_code == "QUEUE_UNAVAILABLE"
    assert "REDIS_URL" not in (failed.safe_error_summary or "")


def test_worker_processes_pair_and_completes_job(app_module, db_session, normal_user, monkeypatch, caplog):
    project, pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    with caplog.at_level("INFO"):
        job = app_module._schedule_project_pair_processing(project.id)
        assert job is not None

        from processing_operations import run_processing_job

        result = run_processing_job(job.id)

    db_session.expire_all()
    refreshed_job = app_module.ProcessingJob.query.get(job.id)
    refreshed_pair = app_module.ProjectPair.query.get(pair.id)
    assert result["ok"] is True
    assert refreshed_job.status == "completed"
    assert refreshed_job.attempt_count == 1
    assert refreshed_pair.is_processed is True
    assert refreshed_pair.feature_extraction_status == "extracted"
    timings = _structured_records(caplog, "processing_timing")
    assert any(payload["event"] == "processing_job_enqueue" for payload in timings)
    pair_payload = next(payload for payload in timings if payload["event"] == "processing_pair")
    job_payload = next(payload for payload in timings if payload["event"] == "processing_job_run")
    assert pair_payload["job_id"] == job.id
    assert pair_payload["project_id"] == project.id
    assert pair_payload["pair_id"] == pair.id
    assert pair_payload["pair_index"] == pair.pair_index
    assert pair_payload["status"] == "completed"
    assert job_payload["job_id"] == job.id
    assert job_payload["project_id"] == project.id
    assert job_payload["job_type"] == "process_project_pairs"
    assert job_payload["pair_count"] == 1
    assert job_payload["attempt_count"] == 1
    assert job_payload["status"] == "completed"
    for payload in timings:
        _assert_timing_payload_is_safe(payload)


def test_worker_replay_completed_job_is_safe(app_module, db_session, normal_user, monkeypatch):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    from processing_operations import run_processing_job
    assert run_processing_job(job.id)["ok"] is True
    assert run_processing_job(job.id)["reason"] == "already_completed"


def test_missing_source_file_is_terminal_safe_error(app_module, db_session, normal_user):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user, tmp_image=False)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    from processing_operations import run_processing_job

    result = run_processing_job(job.id)

    db_session.expire_all()
    failed = app_module.ProcessingJob.query.get(job.id)
    assert result["reason"] == "source_missing"
    assert failed.status == "failed"
    assert failed.safe_error_code == "SOURCE_MISSING"
    assert "F:\\" not in (failed.safe_error_summary or "")


def test_processing_status_endpoint_enforces_user_ownership(client, app_module, db_session, normal_user):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    other = app_module.User(
        email="other-job@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
    )
    db_session.add(other)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = other.id
    assert client.get(f"/api/processing/jobs/{job.id}").status_code == 404

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    response = client.get(f"/api/processing/jobs/{job.id}")
    assert response.status_code == 200
    assert response.get_json()["id"] == job.id


def test_ready_checks_required_queue_without_leaking_url(client, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_REQUIRED", "1")
    monkeypatch.delenv("SCANSTORY_QUEUE_MODE", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload == {"status": "not_ready", "checks": {"database": "ok", "queue": "unavailable"}}


def test_ready_explicit_rq_unavailable_reports_queue_not_ready(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "queue": "unavailable"},
    }


def test_ready_explicit_rq_available_reports_queue_ok(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: True)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ready", "checks": {"database": "ok", "queue": "ok"}}


@pytest.mark.parametrize("mode", ["fake", "inline"])
def test_ready_fake_and_inline_modes_do_not_require_redis(client, app_module, monkeypatch, mode):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", mode)
    monkeypatch.delenv("REDIS_URL", raising=False)

    def redis_should_not_be_checked():
        raise AssertionError("Redis readiness should not be checked for fake/inline queue modes")

    monkeypatch.setattr(app_module, "redis_ready_check", redis_should_not_be_checked)

    response = client.get("/ready")

    assert response.status_code == 200
    # Wave 1 P0-6: readiness now names the queue mode instead of staying silent
    # about it. In a non-production runtime fake/inline are still "ready" and
    # still skip the Redis probe; the difference is that the degraded mode is
    # now visible in the payload rather than indistinguishable from a real queue.
    assert response.get_json() == {"status": "ready", "checks": {"database": "ok", "queue": mode}}


def test_healthz_remains_live_when_rq_redis_unavailable(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_ready_redis_url_selected_rq_mode_requires_redis(client, app_module, monkeypatch):
    monkeypatch.delenv("SCANSTORY_QUEUE_MODE", raising=False)
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_TESTING", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "queue": "unavailable"},
    }


def test_ready_invalid_queue_mode_fails_safely(client, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "bogus")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "queue": "unavailable"},
    }


def test_queue_config_validates_supported_modes(app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "bogus")
    with pytest.raises(app_module.QueueUnavailable):
        app_module.queue_config_summary()


def test_rq_mode_requires_redis_url(app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(app_module.QueueUnavailable) as exc:
        app_module.queue_config_summary()
    assert "REDIS_URL is required" in str(exc.value)


def test_queue_config_summary_is_safe_and_uses_expected_defaults(app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://:secret@127.0.0.1:6379/0")
    monkeypatch.delenv("RQ_QUEUE_NAME", raising=False)
    monkeypatch.delenv("RQ_DEFAULT_TIMEOUT", raising=False)
    summary = app_module.queue_config_summary()
    assert summary == {
        "mode": "rq",
        "redis_configured": True,
        "queue_name": "scanstory-processing",
        "timeout_seconds": 600,
    }
    assert "secret" not in str(summary)


def test_recover_processing_jobs_dry_run_and_apply(app_module, db_session, normal_user):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.status = "processing"
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()
    runner = app_module.app.test_cli_runner()

    dry = runner.invoke(args=["recover-processing-jobs", "--older-than-minutes", "30"])
    assert dry.exit_code == 0
    assert "Mode: dry-run" in dry.output
    assert app_module.ProcessingJob.query.get(job.id).status == "processing"

    applied = runner.invoke(args=["recover-processing-jobs", "--older-than-minutes", "30", "--apply"])
    assert applied.exit_code == 0
    assert app_module.ProcessingJob.query.get(job.id).status == "retrying"


def test_final_failure_after_max_attempts(app_module, db_session, normal_user):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user, tmp_image=False)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.max_attempts = 1
    db_session.commit()

    from processing_operations import run_processing_job
    run_processing_job(job.id)

    db_session.expire_all()
    failed = app_module.ProcessingJob.query.get(job.id)
    assert failed.status == "failed"
    assert failed.attempt_count == 1
