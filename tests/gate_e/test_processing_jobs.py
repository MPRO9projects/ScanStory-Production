from datetime import timedelta

import pytest

from models import ProcessingJob, Workspace, get_utc_now
from processing_jobs import claim_next_job, create_job, fail_job, mark_ready, succeed_job, transition_job, update_progress, JobTransitionError


@pytest.fixture()
def workspace(app_module, db_session):
    workspace = Workspace(public_key="wsp_gate_e_jobs", name="Gate E Jobs")
    db_session.add(workspace)
    db_session.commit()
    return workspace


def test_create_job_and_idempotent_duplicate(app_module, db_session, workspace):
    job, created = create_job(workspace.id, "validate_reference_image", "validate:1")
    duplicate, duplicate_created = create_job(workspace.id, "validate_reference_image", "validate:1")
    assert created is True
    assert duplicate_created is False
    assert duplicate.id == job.id


def test_valid_and_invalid_transitions(app_module, db_session, workspace):
    job, _ = create_job(workspace.id, "probe_video", "probe:1")
    mark_ready(job)
    transition_job(job, "claimed")
    transition_job(job, "running")
    with pytest.raises(JobTransitionError):
        transition_job(job, "ready")
    succeed_job(job)
    assert job.status == "succeeded"


def test_claim_competing_claim_and_lease_expiry(app_module, db_session, workspace):
    job, _ = create_job(workspace.id, "generate_experience_qr", "qr:1", status="ready")
    claimed = claim_next_job("worker-a", lease_seconds=1)
    assert claimed.id == job.id
    assert claim_next_job("worker-b") is None
    claimed.lease_expires_at = get_utc_now() - timedelta(seconds=1)
    db_session.commit()
    reclaimed = claim_next_job("worker-b")
    assert reclaimed.id == job.id
    assert reclaimed.claimed_by == "worker-b"


def test_retry_terminal_failure_cancellation_progress(app_module, db_session, workspace):
    job, _ = create_job(workspace.id, "extract_recognition_artifact", "artifact:1", status="ready", max_attempts=1)
    transition_job(job, "claimed")
    transition_job(job, "running")
    update_progress(job, 25)
    assert job.progress == 25
    fail_job(job, "temporary file lock", retryable=True)
    assert job.status == "retry_scheduled"
    job.attempt_count = 1
    job.status = "running"
    db_session.commit()
    fail_job(job, "corrupt image\nTraceback secret", retryable=False, diagnostics="internal stack")
    assert job.status == "failed_terminal"
    assert "Traceback" in job.error_message
    assert len(job.error_message) <= 500

    cancel, _ = create_job(workspace.id, "probe_video", "cancel:1")
    transition_job(cancel, "cancelled")
    assert cancel.status == "cancelled"


def test_unknown_job_type_rejected(app_module, db_session, workspace):
    with pytest.raises(ValueError):
        create_job(workspace.id, "shell_out_to_anything", "bad:1")
