from datetime import timedelta
from pathlib import Path

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


def test_enqueue_creates_queued_record_and_fake_queue_id(app_module, db_session, normal_user):
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


def test_worker_processes_pair_and_completes_job(app_module, db_session, normal_user, monkeypatch):
    project, pair = _make_project_pair(app_module, db_session, owner_user=normal_user)
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    job, _ = app_module.enqueue_project_pair_processing(project.id)

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
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.delenv("REDIS_URL", raising=False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload == {"status": "not_ready", "checks": {"database": "ok", "queue": "unavailable"}}


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
