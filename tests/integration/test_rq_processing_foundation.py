from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

from public_keys import generate_unique_public_key


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


def test_completed_job_allows_explicit_reprocess_attempt(app_module, db_session, normal_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)

    first, first_created = app_module.enqueue_project_pair_processing(project.id)
    first.status = "completed"
    first.completed_at = app_module.dt.utcnow()
    db_session.commit()

    second, second_created = app_module.enqueue_project_pair_processing(project.id, attempt_scope="reprocess")

    assert first_created is True
    assert second_created is True
    assert second.id != first.id
    assert second.status == "queued"
    assert second.queue_job_id == f"fake-{second.id}"
    assert second.idempotency_key.startswith(f"process_project_pairs:project:{project.id}:pair:-:attempt:")
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 2


@pytest.mark.parametrize("active_status", ["queued", "processing", "running", "retrying"])
def test_explicit_reprocess_reuses_active_attempt(app_module, db_session, normal_user, active_status):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    first, _created = app_module.enqueue_project_pair_processing(project.id)
    first.status = active_status
    first.queue_job_id = f"active-{first.id}"
    db_session.commit()

    second, second_created = app_module.enqueue_project_pair_processing(project.id, attempt_scope="reprocess")

    assert second_created is False
    assert second.id == first.id
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_active_attempt_without_queue_id_is_reused_not_enqueued_again(
    app_module, db_session, normal_user, monkeypatch
):
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _created = app_module.enqueue_project_pair_processing(project.id)
    job.queue_job_id = None
    job.status = "queued"
    db_session.commit()

    def duplicate_transport_should_not_run(_job):
        raise AssertionError("duplicate active request must not enqueue the same job twice")

    monkeypatch.setattr(processing_queue, "_enqueue_transport", duplicate_transport_should_not_run)

    reused, created = app_module.enqueue_project_pair_processing(project.id, attempt_scope="reprocess")

    assert created is False
    assert reused.id == job.id
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_failed_job_allows_explicit_reprocess_and_preserves_history(app_module, db_session, normal_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    failed, _created = app_module.enqueue_project_pair_processing(project.id)
    failed.status = "failed"
    failed.safe_error_code = "SOURCE_MISSING"
    failed.completed_at = app_module.dt.utcnow()
    db_session.commit()

    retry, retry_created = app_module.enqueue_project_pair_processing(project.id, attempt_scope="reprocess")

    assert retry_created is True
    assert retry.id != failed.id
    assert app_module.ProcessingJob.query.get(failed.id).safe_error_code == "SOURCE_MISSING"
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 2


def test_creator_reprocess_route_schedules_new_attempt_after_completed_job(
    client, app_module, db_session, login_user, monkeypatch
):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    project, pair = _make_project_pair(app_module, db_session, owner_user=login_user)
    completed, _created = app_module.enqueue_project_pair_processing(project.id)
    completed.status = "completed"
    completed.completed_at = app_module.dt.utcnow()
    db_session.commit()

    response = client.post(f"/projects/{project.id}/reprocess")

    assert response.status_code == 302
    jobs = app_module.ProcessingJob.query.filter_by(project_id=project.id).order_by(app_module.ProcessingJob.id.asc()).all()
    assert [job.status for job in jobs] == ["completed", "queued"]
    assert jobs[1].idempotency_key.startswith(f"process_project_pairs:project:{project.id}:pair:-:attempt:")
    db_session.refresh(pair)
    assert pair.processing_status == "processing"
    assert pair.feature_extraction_status == "extracting"


def test_creator_double_reprocess_reuses_active_attempt(client, app_module, db_session, login_user, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    project, _pair = _make_project_pair(app_module, db_session, owner_user=login_user)

    first = client.post(f"/projects/{project.id}/reprocess")
    second = client.post(f"/projects/{project.id}/reprocess")

    assert first.status_code == 302
    assert second.status_code == 302
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
    # V1.1 P1-3: rq readiness now also requires a usable worker, so the reachable
    # -Redis case has to state that a worker is attached.
    monkeypatch.setattr(app_module, "queue_worker_state", lambda: ("ok", 1))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ready",
        "checks": {"database": "ok", "queue": "ok", "workers": "ok", "usable_worker_count": 1},
    }


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
    assert "requeued=True" in applied.output
    db_session.expire_all()
    recovered = app_module.ProcessingJob.query.get(job.id)
    assert recovered.status == "retrying"
    assert recovered.queue_job_id == f"fake-{job.id}"


def test_recover_apply_reenqueues_stale_job_through_real_transport(
    app_module, db_session, normal_user, monkeypatch
):
    """The proven bug: --apply must not just flip status - it must re-enqueue."""
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.status = "queued"
    job.queue_job_id = None
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()

    calls = []

    def spy_transport(job_arg):
        calls.append(job_arg.id)
        return "requeued-sentinel"

    monkeypatch.setattr(processing_queue, "_enqueue_transport", spy_transport)

    runner = app_module.app.test_cli_runner()
    applied = runner.invoke(
        args=["recover-processing-jobs", "--job-id", str(job.id), "--older-than-minutes", "0", "--apply"]
    )

    assert applied.exit_code == 0
    assert calls == [job.id]
    db_session.expire_all()
    recovered = app_module.ProcessingJob.query.get(job.id)
    assert recovered.status == "retrying"
    assert recovered.queue_job_id == "requeued-sentinel"
    # Same row, not a new one.
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_recover_apply_is_idempotent_against_a_still_live_rq_job(
    app_module, db_session, normal_user, monkeypatch
):
    """Repeated --apply must not double-enqueue a job that is already live."""
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.status = "queued"
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()

    monkeypatch.setattr(processing_queue, "_stale_job_has_live_queue_entry", lambda job: True)

    def transport_should_not_run(_job):
        raise AssertionError("must not re-enqueue while an equivalent RQ job is still queued/started")

    monkeypatch.setattr(processing_queue, "_enqueue_transport", transport_should_not_run)

    runner = app_module.app.test_cli_runner()
    applied = runner.invoke(
        args=["recover-processing-jobs", "--job-id", str(job.id), "--older-than-minutes", "0", "--apply"]
    )

    assert applied.exit_code == 0
    assert "requeued=False" in applied.output
    db_session.expire_all()
    untouched = app_module.ProcessingJob.query.get(job.id)
    assert untouched.status == "queued"


def test_recover_dry_run_never_enqueues_or_mutates(app_module, db_session, normal_user, monkeypatch):
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    original_queue_job_id = job.queue_job_id
    job.status = "queued"
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()

    def transport_should_not_run(_job):
        raise AssertionError("dry-run must never enqueue")

    monkeypatch.setattr(processing_queue, "_enqueue_transport", transport_should_not_run)

    runner = app_module.app.test_cli_runner()
    dry = runner.invoke(
        args=["recover-processing-jobs", "--job-id", str(job.id), "--older-than-minutes", "0"]
    )

    assert dry.exit_code == 0
    assert "Mode: dry-run" in dry.output
    db_session.expire_all()
    unchanged = app_module.ProcessingJob.query.get(job.id)
    assert unchanged.status == "queued"
    assert unchanged.queue_job_id == original_queue_job_id


def test_recover_apply_marks_exhausted_job_failed_without_enqueue(
    app_module, db_session, normal_user, monkeypatch
):
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.status = "processing"
    job.attempt_count = job.max_attempts
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()

    def transport_should_not_run(_job):
        raise AssertionError("a retry-exhausted job must not be re-enqueued")

    monkeypatch.setattr(processing_queue, "_enqueue_transport", transport_should_not_run)

    runner = app_module.app.test_cli_runner()
    applied = runner.invoke(
        args=["recover-processing-jobs", "--job-id", str(job.id), "--older-than-minutes", "0", "--apply"]
    )

    assert applied.exit_code == 0
    db_session.expire_all()
    failed = app_module.ProcessingJob.query.get(job.id)
    assert failed.status == "failed"
    assert failed.safe_error_code == "STALE_JOB_FAILED"


def test_recover_apply_leaves_truthful_state_when_reenqueue_fails(
    app_module, db_session, normal_user, monkeypatch
):
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    job, _ = app_module.enqueue_project_pair_processing(project.id)
    job.status = "queued"
    job.last_heartbeat_at = app_module.dt.utcnow() - timedelta(hours=2)
    db_session.commit()

    def transport_raises(_job):
        raise processing_queue.QueueUnavailable("redis down")

    monkeypatch.setattr(processing_queue, "_enqueue_transport", transport_raises)

    runner = app_module.app.test_cli_runner()
    applied = runner.invoke(
        args=["recover-processing-jobs", "--job-id", str(job.id), "--older-than-minutes", "0", "--apply"]
    )

    assert applied.exit_code == 0
    assert "requeued=False" in applied.output
    db_session.expire_all()
    row = app_module.ProcessingJob.query.get(job.id)
    # Recoverable, not a false "a worker has it" - and still visibly stale so
    # the very next recovery pass tries again rather than needing a human.
    assert row.status == "retrying"
    assert row.safe_error_code == "STALE_JOB_REQUEUE_FAILED"
    assert row.last_heartbeat_at < app_module.dt.utcnow() - timedelta(minutes=1)


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


# ===========================================================================
# Media replacement reprocessing: a completed project's FIRST ProcessingJob
# permanently occupies the "initial" idempotency key
# (process_project_pairs:project:<id>:pair:-). Replacing media later is a new
# logical processing generation, not that same attempt reborn - it must not
# collide with the historical row on uq_processing_job_project_idempotency.
# ===========================================================================
def _complete(job, db_session, app_module):
    job.status = "completed"
    job.completed_at = app_module.dt.utcnow()
    db_session.commit()


def test_repeated_edit_reprocess_after_completion_does_not_collide(
    app_module, db_session, normal_user
):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    initial_job, _ = app_module.enqueue_project_pair_processing(project.id)
    _complete(initial_job, db_session, app_module)

    # This is what user_edit_project now does on a media replacement - the
    # exact call the reported UniqueViolation crashed on.
    reprocess_job, created = app_module.enqueue_project_pair_processing(
        project.id, attempt_scope="reprocess"
    )

    assert created is True
    assert reprocess_job.id != initial_job.id
    assert reprocess_job.idempotency_key != initial_job.idempotency_key
    db_session.expire_all()
    assert app_module.ProcessingJob.query.get(initial_job.id).status == "completed"
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 2


def test_sequential_replacement_generations_do_not_collide_with_each_other(
    app_module, db_session, normal_user
):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    initial_job, _ = app_module.enqueue_project_pair_processing(project.id)
    _complete(initial_job, db_session, app_module)

    first_replacement, created_1 = app_module.enqueue_project_pair_processing(
        project.id, attempt_scope="reprocess"
    )
    _complete(first_replacement, db_session, app_module)

    second_replacement, created_2 = app_module.enqueue_project_pair_processing(
        project.id, attempt_scope="reprocess"
    )

    assert created_1 is True
    assert created_2 is True
    keys = {
        initial_job.idempotency_key,
        first_replacement.idempotency_key,
        second_replacement.idempotency_key,
    }
    assert len(keys) == 3  # all three generations are distinct
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 3


def test_duplicate_replacement_submission_before_completion_collapses_to_one_job(
    app_module, db_session, normal_user
):
    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    initial_job, _ = app_module.enqueue_project_pair_processing(project.id)
    _complete(initial_job, db_session, app_module)

    first, created_1 = app_module.enqueue_project_pair_processing(
        project.id, attempt_scope="reprocess"
    )
    # A second submission of the SAME replacement (double-click, retried POST)
    # arrives while the first is still active - must reuse it, not enqueue a
    # second worker operation for the same generation.
    second, created_2 = app_module.enqueue_project_pair_processing(
        project.id, attempt_scope="reprocess"
    )

    assert created_1 is True
    assert created_2 is False
    assert second.id == first.id
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 2


def test_create_processing_job_resolves_integrity_error_to_active_winner(
    app_module, db_session, normal_user, monkeypatch
):
    import processing_queue

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    winner = app_module.ProcessingJob(
        public_key=generate_unique_public_key(db_session, app_module.ProcessingJob, "job"),
        job_type="process_project_pairs",
        status="queued",
        project_id=project.id,
        idempotency_key=f"process_project_pairs:project:{project.id}:pair:-",
        max_attempts=3,
    )
    db_session.add(winner)
    db_session.commit()

    # Simulate a genuine race: by the time create_processing_job decides to
    # insert, a concurrent request already committed the identical
    # (project_id, idempotency_key) row - the active_project_job() pre-check
    # ran a moment too early and saw nothing yet.
    monkeypatch.setattr(processing_queue, "active_project_job", lambda *a, **k: None)

    job, created = processing_queue.create_processing_job(
        "process_project_pairs", project_id=project.id, attempt_scope="initial"
    )

    assert created is False
    assert job.id == winner.id
    db_session.expire_all()
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_create_processing_job_does_not_report_false_success_on_terminal_collision(
    app_module, db_session, normal_user, monkeypatch
):
    """A collision against a TERMINAL row is not "already scheduled" - nothing
    is actually going to process this. Must not swallow the conflict."""
    import processing_queue
    from sqlalchemy.exc import IntegrityError

    project, _pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    stale = app_module.ProcessingJob(
        public_key=generate_unique_public_key(db_session, app_module.ProcessingJob, "job"),
        job_type="process_project_pairs",
        status="completed",
        project_id=project.id,
        idempotency_key=f"process_project_pairs:project:{project.id}:pair:-",
        max_attempts=3,
    )
    db_session.add(stale)
    db_session.commit()

    monkeypatch.setattr(processing_queue, "active_project_job", lambda *a, **k: None)

    with pytest.raises(IntegrityError):
        processing_queue.create_processing_job(
            "process_project_pairs", project_id=project.id, attempt_scope="initial"
        )

    # The session must still be usable afterward - not left poisoned.
    db_session.rollback()
    assert app_module.ProcessingJob.query.filter_by(project_id=project.id).count() == 1


def test_edit_project_media_replacement_reprocesses_after_prior_completion(
    app_module, db_session, normal_user, login_user, client
):
    """End-to-end through the real /projects/<id>/edit route - the exact
    reported reproduction: a ready project, media replaced, submit."""
    project, pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    initial_job, _ = app_module.enqueue_project_pair_processing(project.id)
    _complete(initial_job, db_session, app_module)
    pair.is_processed = True
    pair.processing_status = "completed"
    db_session.commit()

    # An image replacement is what actually flips is_processed back to False
    # and puts the pair back on the reprocessing path (a video-only swap needs
    # no feature re-extraction, since ORB features come from the image).
    replacement_image = BytesIO()
    Image.new("RGB", (640, 480), (10, 20, 30)).save(replacement_image, format="JPEG", quality=88)
    replacement_image.seek(0)

    response = client.post(
        f"/projects/{project.id}/edit",
        data={"image_0": (replacement_image, "replacement.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code in (200, 302)
    db_session.expire_all()
    jobs = (
        app_module.ProcessingJob.query.filter_by(project_id=project.id)
        .order_by(app_module.ProcessingJob.id)
        .all()
    )
    assert len(jobs) == 2
    assert jobs[0].id == initial_job.id
    assert jobs[0].status == "completed"
    assert jobs[1].idempotency_key != jobs[0].idempotency_key
    assert jobs[1].status in ("queued", "processing", "completed")
