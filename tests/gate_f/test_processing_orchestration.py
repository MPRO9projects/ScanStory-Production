import pytest

from models import Asset, Experience, ProcessingEvent, ProcessingJob, Trigger, TriggerAsset, Workspace, WorkspaceMember, get_utc_now
from processing_jobs import claim_next_job, fail_job, transition_job
from processing_orchestration import (
    activate_artifact_if_current,
    cancel_trigger_processing,
    get_experience_processing_status,
    get_trigger_processing_status,
    orchestrate_experience_processing,
    orchestrate_trigger_processing,
    record_source_replaced,
    regenerate_qr_for_experience,
    regenerate_recognition_for_trigger,
    retry_failed_trigger_processing,
)


@pytest.fixture()
def experience_with_triggers(app_module, db_session, normal_user):
    workspace = Workspace(public_key="wsp_gate_f", name="Gate F")
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=normal_user.id, role="owner"))
    experience = Experience(public_key="exp_gate_f", workspace_id=workspace.id, name="Gate F")
    db_session.add(experience)
    db_session.flush()
    triggers = []
    for idx in range(5):
        trigger = Trigger(public_key=f"trg_gate_f_{idx}", experience_id=experience.id, name=f"Trigger {idx}", status="draft")
        db_session.add(trigger)
        db_session.flush()
        image = Asset(public_key=f"ast_gate_f_i_{idx}", workspace_id=workspace.id, asset_type="image", storage_key=f"image-{idx}.jpg")
        video = Asset(public_key=f"ast_gate_f_v_{idx}", workspace_id=workspace.id, asset_type="video", storage_key=f"video-{idx}.mp4")
        db_session.add_all([image, video])
        db_session.flush()
        db_session.add_all([
            TriggerAsset(trigger_id=trigger.id, asset_id=image.id, role="reference_image"),
            TriggerAsset(trigger_id=trigger.id, asset_id=video.id, role="video"),
        ])
        triggers.append(trigger)
    db_session.commit()
    return experience, triggers


def test_new_trigger_orchestration_and_duplicate_request(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    first = orchestrate_trigger_processing(triggers[0].id, requested_by="creator")
    second = orchestrate_trigger_processing(triggers[0].id, requested_by="creator")
    assert set(first["scheduled"]) == {
        "validate_reference_image",
        "probe_video",
        "extract_recognition_artifact",
        "test_marker_robustness",
        "verify_processing_readiness",
    }
    assert second["scheduled"] == []
    assert ProcessingJob.query.filter_by(trigger_id=triggers[0].id).count() == 5
    assert ProcessingEvent.query.filter_by(trigger_id=triggers[0].id, event_type="processing_requested").count() == 2


def test_experience_orchestration_ignores_excluded_and_missing_asset(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    triggers[1].is_excluded = True
    TriggerAsset.query.filter_by(trigger_id=triggers[2].id, role="video").delete()
    db_session.commit()
    result = orchestrate_experience_processing(experience.id)
    assert triggers[1].id not in result["scheduled"]
    assert "missing_video" in orchestrate_trigger_processing(triggers[2].id)["skipped"]


def test_selective_reprocessing_image_vs_video(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_trigger_processing(triggers[0].id)
    before = ProcessingJob.query.count()
    image_result = record_source_replaced(triggers[0].id, "reference_image", "new-image.jpg")
    assert "extract_recognition_artifact" in image_result["scheduled"]
    video_result = record_source_replaced(triggers[1].id, "video", "new-video.mp4")
    assert "probe_video" in video_result["scheduled"]
    assert "extract_recognition_artifact" not in video_result["scheduled"]
    assert ProcessingJob.query.count() > before


def test_regenerate_recognition_and_qr_preserve_destination(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    rec = regenerate_recognition_for_trigger(triggers[0].id)
    qr = regenerate_qr_for_experience(experience.id, "https://example.test/permanent")
    qr_again = regenerate_qr_for_experience(experience.id, "https://example.test/permanent")
    assert rec["scheduled"] == ["regenerate_recognition_artifact"]
    assert qr["destination"] == "https://example.test/permanent"
    assert qr_again["scheduled"] == []


def test_creator_safe_status_and_diagnostics_separation(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_trigger_processing(triggers[0].id)
    job = ProcessingJob.query.filter_by(trigger_id=triggers[0].id).first()
    transition_job(job, "claimed")
    transition_job(job, "running")
    fail_job(job, "absolute path C:/secret/file.jpg\nTraceback details", retryable=False, diagnostics="internal stack")
    status = get_trigger_processing_status(triggers[0].id)
    diagnostic = get_trigger_processing_status(triggers[0].id, include_diagnostics=True)
    assert status["status"] == "Needs Attention"
    assert "diagnostics" not in status
    assert diagnostic["diagnostics"]


def test_progress_aggregation_and_all_ready(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_experience_processing(experience.id)
    for job in ProcessingJob.query.all():
        job.status = "succeeded"
    for trigger in triggers:
        trigger.status = "ready"
    db_session.commit()
    summary = get_experience_processing_status(experience.id)
    assert summary["ready"] == 5
    assert summary["processing_ready"] is True


def test_cancellation_is_trigger_scoped(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_trigger_processing(triggers[0].id)
    orchestrate_trigger_processing(triggers[1].id)
    result = cancel_trigger_processing(triggers[0].id)
    assert result["cancelled"] == 5
    assert ProcessingJob.query.filter_by(trigger_id=triggers[1].id, status="ready").count() == 5


def test_crash_recovery_and_competing_workers(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_trigger_processing(triggers[0].id)
    first = claim_next_job("worker-a", lease_seconds=1)
    assert first is not None
    assert claim_next_job("worker-b") is not None
    first.lease_expires_at = get_utc_now()
    db_session.commit()
    reclaimed = claim_next_job("worker-c")
    assert reclaimed is not None


def test_stale_job_cannot_activate_old_output(app_module, db_session, experience_with_triggers):
    experience, triggers = experience_with_triggers
    orchestrate_trigger_processing(triggers[0].id)
    job = ProcessingJob.query.filter_by(trigger_id=triggers[0].id, job_type="extract_recognition_artifact").first()
    assert activate_artifact_if_current(job, "newer-source-hash") is False
    assert job.status == "failed_terminal"


def test_authorization_own_workspace_and_other_denied(app_module, db_session, normal_user, expired_user, experience_with_triggers):
    experience, triggers = experience_with_triggers
    assert get_experience_processing_status(experience.id, user_id=normal_user.id)["found"] is True
    with pytest.raises(PermissionError):
        get_experience_processing_status(experience.id, user_id=expired_user.id)


def test_30_and_100_trigger_status_is_bounded(app_module, db_session):
    workspace = Workspace(public_key="wsp_gate_f_big", name="Big")
    db_session.add(workspace)
    db_session.flush()
    experience = Experience(public_key="exp_gate_f_big", workspace_id=workspace.id, name="Big")
    db_session.add(experience)
    db_session.flush()
    for idx in range(100):
        db_session.add(Trigger(public_key=f"trg_big_{idx}", experience_id=experience.id, name=f"T{idx}", status="ready"))
    db_session.commit()
    summary = get_experience_processing_status(experience.id)
    assert summary["trigger_count"] == 100
    assert summary["response_truncated"] is False
