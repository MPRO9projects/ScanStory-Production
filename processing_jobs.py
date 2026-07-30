import json
import traceback
from datetime import datetime, timedelta

from public_keys import generate_unique_public_key
from models import ProcessingJob, db, get_utc_now


JOB_TYPES = {
    "validate_reference_image",
    "probe_video",
    "generate_poster",
    "extract_recognition_artifact",
    "test_marker_robustness",
    "generate_experience_qr",
    "generate_trigger_qr",
    "regenerate_recognition_artifact",
    "regenerate_qr_asset",
    "verify_processing_readiness",
}

ALLOWED_TRANSITIONS = {
    "pending": {"ready", "cancelled"},
    "ready": {"claimed", "cancelled"},
    "claimed": {"running", "ready", "cancelled"},
    "running": {"succeeded", "failed_retryable", "failed_terminal"},
    "failed_retryable": {"retry_scheduled", "failed_terminal"},
    "retry_scheduled": {"ready", "cancelled"},
    "succeeded": set(),
    "failed_terminal": set(),
    "cancelled": set(),
}

SAFE_ERROR_LIMIT = 500


class JobTransitionError(ValueError):
    pass


def sanitize_error(message):
    text = str(message or "processing failed").replace("\x00", "")
    return text[:SAFE_ERROR_LIMIT]


def create_job(workspace_id, job_type, idempotency_key, experience_id=None, trigger_id=None, status="pending", priority=100, max_attempts=3):
    if job_type not in JOB_TYPES:
        raise ValueError("unknown job type")
    existing = ProcessingJob.query.filter_by(workspace_id=workspace_id, idempotency_key=idempotency_key).first()
    if existing:
        return existing, False
    job = ProcessingJob(
        public_key=generate_unique_public_key(db.session, ProcessingJob, "job"),
        workspace_id=workspace_id,
        experience_id=experience_id,
        trigger_id=trigger_id,
        job_type=job_type,
        status=status,
        priority=priority,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        available_at=get_utc_now(),
    )
    db.session.add(job)
    db.session.commit()
    return job, True


def transition_job(job, new_status):
    if new_status not in ALLOWED_TRANSITIONS.get(job.status, set()):
        raise JobTransitionError(f"invalid transition {job.status} -> {new_status}")
    job.status = new_status
    if new_status == "running":
        job.started_at = get_utc_now()
    elif new_status in {"succeeded", "failed_terminal", "cancelled"}:
        job.completed_at = get_utc_now()
    db.session.commit()
    return job


def mark_ready(job):
    return transition_job(job, "ready")


def claim_next_job(worker_id, lease_seconds=60):
    now = get_utc_now()
    job = (
        ProcessingJob.query.filter(
            ProcessingJob.status.in_(["ready", "retry_scheduled", "claimed", "running"]),
            db.or_(ProcessingJob.available_at.is_(None), ProcessingJob.available_at <= now),
            db.or_(ProcessingJob.lease_expires_at.is_(None), ProcessingJob.lease_expires_at <= now),
        )
        .order_by(ProcessingJob.priority.asc(), ProcessingJob.created_at.asc())
        .first()
    )
    if not job:
        return None
    if job.status == "retry_scheduled":
        job.status = "ready"
        db.session.flush()
    if job.status in {"claimed", "running"} and job.lease_expires_at and job.lease_expires_at <= now:
        job.status = "ready"
        db.session.flush()
    if job.status != "ready":
        return None
    job.status = "claimed"
    job.claimed_by = worker_id
    job.claimed_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    db.session.commit()
    return job


def fail_job(job, error, retryable=True, retry_delay_seconds=60, diagnostics=None):
    job.error_message = sanitize_error(error)
    job.internal_diagnostics = sanitize_error(diagnostics or traceback.format_exc())
    if retryable and job.attempt_count < job.max_attempts:
        if job.status == "running":
            transition_job(job, "failed_retryable")
        job.status = "retry_scheduled"
        job.available_at = get_utc_now() + timedelta(seconds=retry_delay_seconds)
    else:
        if job.status == "running":
            job.status = "failed_terminal"
        else:
            job.status = "failed_terminal"
        job.completed_at = get_utc_now()
    db.session.commit()
    return job


def succeed_job(job, progress=100):
    job.progress = progress
    if job.status == "running":
        transition_job(job, "succeeded")
    else:
        job.status = "succeeded"
        job.completed_at = get_utc_now()
        db.session.commit()
    return job


def update_progress(job, progress):
    if progress < 0 or progress > 100:
        raise ValueError("progress must be between 0 and 100")
    job.progress = progress
    db.session.commit()
    return job


def job_log(job, event):
    return json.dumps(
        {
            "event": event,
            "job_id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "worker": job.claimed_by,
        },
        sort_keys=True,
    )
