import hashlib
import json
from dataclasses import dataclass

from models import Asset, Experience, ProcessingEvent, ProcessingJob, RecognitionArtifact, Trigger, TriggerAsset, WorkspaceMember, db
from processing_jobs import create_job, fail_job, mark_ready, transition_job
from processing_readiness import summarize_experience_processing
from public_keys import generate_unique_public_key


PIPELINE_VERSION = "gate-f-v1"
ALGORITHM_VERSION = "orb-gate-e-v1"
PROGRESS_WEIGHTS = {
    "validate_reference_image": 15,
    "probe_video": 15,
    "extract_recognition_artifact": 40,
    "test_marker_robustness": 20,
    "verify_processing_readiness": 10,
}


CREATOR_TRIGGER_STATUS = {
    "draft": "Draft",
    "uploading": "Uploading",
    "validating": "Validating Image",
    "optimizing": "Checking Video",
    "extracting": "Generating Recognition",
    "robustness_testing": "Testing Recognition",
    "ready": "Ready",
    "failed": "Needs Attention",
    "retry_scheduled": "Retry Scheduled",
    "retrying": "Retry Scheduled",
    "excluded": "Excluded",
}


@dataclass
class SourceState:
    reference_image_hash: str | None
    video_hash: str | None
    algorithm_version: str
    pipeline_version: str
    qr_destination: str | None = None
    qr_style_version: str = "default"


def _hash_text(value):
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _event(event_type, trigger=None, experience=None, job=None, actor=None, creator_message=None, diagnostic_code=None, diagnostic=None):
    event = ProcessingEvent(
        workspace_id=(experience.workspace_id if experience else (trigger.experience.workspace_id if trigger else None)),
        experience_id=(experience.id if experience else (trigger.experience_id if trigger else None)),
        trigger_id=trigger.id if trigger else None,
        job_id=job.id if job else None,
        event_type=event_type,
        actor=actor,
        creator_message=creator_message,
        diagnostic_code=diagnostic_code,
        diagnostic_json=json.dumps(diagnostic or {}, sort_keys=True),
    )
    db.session.add(event)
    db.session.commit()
    return event


def _assets_for_trigger(trigger):
    roles = {link.role: link.asset for link in trigger.trigger_assets}
    return roles.get("reference_image"), roles.get("video")


def _state_for_trigger(trigger, qr_destination=None, qr_style_version="default"):
    image, video = _assets_for_trigger(trigger)
    return SourceState(
        reference_image_hash=_hash_text(image.storage_key if image else None),
        video_hash=_hash_text(video.storage_key if video else None),
        algorithm_version=ALGORITHM_VERSION,
        pipeline_version=PIPELINE_VERSION,
        qr_destination=qr_destination,
        qr_style_version=qr_style_version,
    )


def _key(job_type, trigger_id=None, experience_id=None, source=None):
    parts = [job_type, f"trg:{trigger_id}" if trigger_id else f"exp:{experience_id}"]
    if source:
        parts.extend([
            source.reference_image_hash or "",
            source.video_hash or "",
            source.algorithm_version,
            source.pipeline_version,
            source.qr_destination or "",
            source.qr_style_version,
        ])
    return ":".join(parts)


def _schedule(workspace_id, job_type, idempotency_key, experience_id=None, trigger_id=None, requested_by=None):
    job, created = create_job(
        workspace_id=workspace_id,
        job_type=job_type,
        idempotency_key=idempotency_key,
        experience_id=experience_id,
        trigger_id=trigger_id,
        status="pending",
    )
    if created:
        mark_ready(job)
        trigger = Trigger.query.get(trigger_id) if trigger_id else None
        experience = Experience.query.get(experience_id) if experience_id else None
        _event("job_created", trigger=trigger, experience=experience, job=job, actor=requested_by, creator_message="Processing step queued")
    return job, created


def orchestrate_trigger_processing(trigger_id, reason="initial", requested_by="system", source_override=None):
    trigger = Trigger.query.get(trigger_id)
    if not trigger:
        raise ValueError("trigger not found")
    if trigger.is_excluded:
        _event("processing_requested", trigger=trigger, actor=requested_by, creator_message="Trigger is excluded")
        return {"scheduled": [], "skipped": ["excluded"], "summary": get_trigger_processing_status(trigger.id)}

    image, video = _assets_for_trigger(trigger)
    scheduled = []
    skipped = []
    source = source_override or _state_for_trigger(trigger)
    _event("processing_requested", trigger=trigger, actor=requested_by, creator_message="Processing requested", diagnostic={"reason": reason})

    if not image:
        skipped.append("missing_reference_image")
    else:
        for job_type in ("validate_reference_image", "extract_recognition_artifact", "test_marker_robustness"):
            job, created = _schedule(trigger.experience.workspace_id, job_type, _key(job_type, trigger_id=trigger.id, source=source), trigger_id=trigger.id, experience_id=trigger.experience_id, requested_by=requested_by)
            if created:
                scheduled.append(job_type)

    if not video:
        skipped.append("missing_video")
    else:
        job, created = _schedule(trigger.experience.workspace_id, "probe_video", _key("probe_video", trigger_id=trigger.id, source=source), trigger_id=trigger.id, experience_id=trigger.experience_id, requested_by=requested_by)
        if created:
            scheduled.append("probe_video")

    job, created = _schedule(trigger.experience.workspace_id, "verify_processing_readiness", _key("verify_processing_readiness", trigger_id=trigger.id, source=source), trigger_id=trigger.id, experience_id=trigger.experience_id, requested_by=requested_by)
    if created:
        scheduled.append("verify_processing_readiness")

    return {"scheduled": scheduled, "skipped": skipped, "summary": get_trigger_processing_status(trigger.id)}


def orchestrate_experience_processing(experience_id, requested_by="system"):
    experience = Experience.query.get(experience_id)
    if not experience:
        raise ValueError("experience not found")
    scheduled = {}
    for trigger in Trigger.query.filter_by(experience_id=experience.id).all():
        if trigger.is_excluded:
            continue
        result = orchestrate_trigger_processing(trigger.id, reason="experience", requested_by=requested_by)
        scheduled[trigger.id] = result["scheduled"]
    _event("processing_requested", experience=experience, actor=requested_by, creator_message="Experience processing requested")
    return {"scheduled": scheduled, "summary": get_experience_processing_status(experience.id)}


def retry_failed_trigger_processing(trigger_id, requested_by="system"):
    trigger = Trigger.query.get(trigger_id)
    if not trigger:
        raise ValueError("trigger not found")
    failed = ProcessingJob.query.filter_by(trigger_id=trigger.id, status="failed_terminal").all()
    for job in failed:
        job.status = "ready"
        job.error_message = None
        job.internal_diagnostics = None
    db.session.commit()
    _event("manual_retry_requested", trigger=trigger, actor=requested_by, creator_message="Retry requested")
    return orchestrate_trigger_processing(trigger.id, reason="retry", requested_by=requested_by)


def regenerate_recognition_for_trigger(trigger_id, requested_by="system", algorithm_version=ALGORITHM_VERSION):
    trigger = Trigger.query.get(trigger_id)
    source = _state_for_trigger(trigger)
    source.algorithm_version = algorithm_version
    job, created = _schedule(trigger.experience.workspace_id, "regenerate_recognition_artifact", _key("regenerate_recognition_artifact", trigger_id=trigger.id, source=source), trigger_id=trigger.id, experience_id=trigger.experience_id, requested_by=requested_by)
    _event("artifact_regenerated", trigger=trigger, job=job, actor=requested_by, creator_message="Recognition regeneration queued")
    return {"scheduled": ["regenerate_recognition_artifact"] if created else [], "job_id": job.id}


def regenerate_qr_for_experience(experience_id, destination_url, requested_by="system", style_version="default"):
    experience = Experience.query.get(experience_id)
    source = SourceState(None, None, ALGORITHM_VERSION, PIPELINE_VERSION, qr_destination=destination_url, qr_style_version=style_version)
    job, created = _schedule(experience.workspace_id, "regenerate_qr_asset", _key("regenerate_qr_asset", experience_id=experience.id, source=source), experience_id=experience.id, requested_by=requested_by)
    _event("qr_asset_regenerated", experience=experience, job=job, actor=requested_by, creator_message="QR asset regeneration queued", diagnostic={"destination_hash": _hash_text(destination_url)})
    return {"scheduled": ["regenerate_qr_asset"] if created else [], "destination": destination_url, "job_id": job.id}


def record_source_replaced(trigger_id, source_type, storage_key, requested_by="system"):
    trigger = Trigger.query.get(trigger_id)
    image, video = _assets_for_trigger(trigger)
    if source_type == "reference_image" and image:
        image.storage_key = storage_key
    elif source_type == "video" and video:
        video.storage_key = storage_key
    db.session.commit()
    _event("source_replaced", trigger=trigger, actor=requested_by, creator_message=f"{source_type} replaced", diagnostic={"storage_key": storage_key})
    if source_type == "reference_image":
        return orchestrate_trigger_processing(trigger_id, reason="reference_image_changed", requested_by=requested_by)
    if source_type == "video":
        source = _state_for_trigger(trigger)
        scheduled = []
        for job_type in ("probe_video", "verify_processing_readiness"):
            job, created = _schedule(trigger.experience.workspace_id, job_type, _key(job_type, trigger_id=trigger.id, source=source), trigger_id=trigger.id, experience_id=trigger.experience_id, requested_by=requested_by)
            if created:
                scheduled.append(job_type)
        return {"scheduled": scheduled, "summary": get_trigger_processing_status(trigger_id)}
    raise ValueError("unknown source type")


def cancel_trigger_processing(trigger_id, requested_by="system"):
    trigger = Trigger.query.get(trigger_id)
    cancellable = ProcessingJob.query.filter(
        ProcessingJob.trigger_id == trigger_id,
        ProcessingJob.status.in_(["pending", "ready", "retry_scheduled"]),
    ).all()
    for job in cancellable:
        job.status = "cancelled"
    db.session.commit()
    _event("processing_cancelled", trigger=trigger, actor=requested_by, creator_message="Processing cancelled")
    return {"cancelled": len(cancellable)}


def activate_artifact_if_current(job, source_key):
    expected = source_key in job.idempotency_key
    if not expected:
        fail_job(job, "stale source result ignored", retryable=False, diagnostics="stale_source")
        return False
    return True


def get_trigger_processing_status(trigger_id, include_diagnostics=False, user_id=None):
    trigger = Trigger.query.get(trigger_id)
    if not trigger:
        return {"found": False}
    if user_id is not None:
        allowed = (
            WorkspaceMember.query.filter_by(workspace_id=trigger.experience.workspace_id, user_id=user_id, status="active").first()
            is not None
        )
        if not allowed:
            raise PermissionError("not authorized")
    jobs = ProcessingJob.query.filter_by(trigger_id=trigger.id).order_by(ProcessingJob.created_at.desc()).limit(20).all()
    failed = [job for job in jobs if job.status in {"failed_terminal", "failed_retryable"}]
    processing = [job for job in jobs if job.status in {"pending", "ready", "claimed", "running", "retry_scheduled"}]
    status = "Excluded" if trigger.is_excluded else CREATOR_TRIGGER_STATUS.get(trigger.status, "Draft")
    if failed:
        status = "Needs Attention"
    elif processing:
        latest_type = processing[0].job_type
        status = {
            "validate_reference_image": "Validating Image",
            "probe_video": "Checking Video",
            "extract_recognition_artifact": "Generating Recognition",
            "regenerate_recognition_artifact": "Generating Recognition",
            "test_marker_robustness": "Testing Recognition",
        }.get(latest_type, status)
    payload = {
        "found": True,
        "trigger_id": trigger.id,
        "status": status,
        "creator_message": _creator_message(status),
        "progress": calculate_trigger_progress(trigger.id),
        "warnings": [job.error_message for job in failed if job.error_message],
        "diagnostic_id": failed[0].public_key if failed else None,
    }
    if include_diagnostics:
        payload["diagnostics"] = [
            {"job_type": job.job_type, "attempt_count": job.attempt_count, "worker": job.claimed_by, "code": job.error_code}
            for job in jobs
        ]
    return payload


def get_experience_processing_status(experience_id, user_id=None):
    experience = Experience.query.get(experience_id)
    if not experience:
        return {"found": False}
    if user_id is not None:
        allowed = WorkspaceMember.query.filter_by(workspace_id=experience.workspace_id, user_id=user_id, status="active").first() is not None
        if not allowed:
            raise PermissionError("not authorized")
    base = summarize_experience_processing(experience.id)
    trigger_statuses = [get_trigger_processing_status(t.id) for t in Trigger.query.filter_by(experience_id=experience.id).limit(100).all()]
    needs_attention = sum(1 for item in trigger_statuses if item["status"] == "Needs Attention")
    processing = sum(1 for item in trigger_statuses if item["status"] in {"Validating Image", "Checking Video", "Generating Recognition", "Testing Recognition", "Retry Scheduled"})
    return {
        "found": True,
        "experience_id": experience.id,
        "trigger_count": base["trigger_count"],
        "ready": base["ready"],
        "processing": processing,
        "needs_attention": needs_attention,
        "excluded": base["excluded"],
        "processing_ready": base["processing_ready"],
        "summary": f"{base['ready']} Ready, {processing} Processing, {needs_attention} Needs Attention",
        "response_truncated": base["trigger_count"] > 100,
    }


def calculate_trigger_progress(trigger_id):
    jobs = ProcessingJob.query.filter_by(trigger_id=trigger_id).all()
    if not jobs:
        return 0
    total = sum(PROGRESS_WEIGHTS.get(job.job_type, 0) for job in jobs)
    if total <= 0:
        return 0
    complete = sum(PROGRESS_WEIGHTS.get(job.job_type, 0) for job in jobs if job.status == "succeeded")
    return min(100, int((complete / total) * 100))


def _creator_message(status):
    return {
        "Ready": "Trigger is ready.",
        "Needs Attention": "Review the media and retry processing.",
        "Validating Image": "Checking the reference image.",
        "Checking Video": "Checking the video.",
        "Generating Recognition": "Generating recognition data.",
        "Testing Recognition": "Testing recognition quality.",
        "Retry Scheduled": "A retry is scheduled.",
        "Excluded": "This Trigger is excluded.",
    }.get(status, "Processing status is available.")
