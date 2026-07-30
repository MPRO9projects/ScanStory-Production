import hashlib
import json

from feature_flags import (
    experience_pause_enabled,
    experience_publishing_enabled,
    public_experience_route_enabled,
    version_rollback_enabled,
)
from models import (
    Asset,
    Experience,
    ExperienceVersion,
    ExperienceVersionTrigger,
    ProcessingEvent,
    ProcessingJob,
    RecognitionArtifact,
    Trigger,
    TriggerAsset,
    WorkspaceMember,
    db,
    get_utc_now,
)


PUBLISH_ROLES = {"owner", "admin", "publisher"}
MANAGE_ROLES = PUBLISH_ROLES | {"creator"}
PROCESSING_STATUSES = {"pending", "ready", "claimed", "running", "retry_scheduled", "failed_retryable"}


class PublishingError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


def permanent_destination(experience):
    return f"/e/{experience.public_key}"


def _member(user_id, workspace_id, roles):
    return WorkspaceMember.query.filter(
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.status == "active",
        WorkspaceMember.role.in_(roles),
    ).first()


def require_publish_permission(experience, user_id):
    if not _member(user_id, experience.workspace_id, PUBLISH_ROLES):
        raise AuthorizationError("publish permission required")


def require_manage_permission(experience, user_id):
    if not _member(user_id, experience.workspace_id, MANAGE_ROLES):
        raise AuthorizationError("manage permission required")


def _event(event_type, experience, version=None, actor=None, message=None, details=None):
    db.session.add(
        ProcessingEvent(
            workspace_id=experience.workspace_id,
            experience_id=experience.id,
            event_type=event_type,
            actor=str(actor) if actor is not None else None,
            creator_message=message,
            diagnostic_json=json.dumps(details or {}, sort_keys=True),
        )
    )


def _asset_for(trigger, role):
    link = TriggerAsset.query.filter_by(trigger_id=trigger.id, role=role).order_by(TriggerAsset.id.desc()).first()
    return link.asset if link else None


def _recognition_for(trigger):
    return RecognitionArtifact.query.filter_by(trigger_id=trigger.id, status="available").order_by(RecognitionArtifact.id.desc()).first()


def _required_jobs_succeeded(trigger):
    required = {"validate_reference_image", "probe_video", "extract_recognition_artifact", "test_marker_robustness", "verify_processing_readiness"}
    rows = ProcessingJob.query.filter(ProcessingJob.trigger_id == trigger.id, ProcessingJob.job_type.in_(required)).all()
    latest = {}
    for job in rows:
        latest[job.job_type] = job.status
    return all(latest.get(job_type) == "succeeded" for job_type in required), latest


def _draft_for_edit(experience):
    return (
        ExperienceVersion.query.filter_by(experience_id=experience.id, status="draft")
        .order_by(ExperienceVersion.version_number.desc())
        .first()
    )


def ensure_draft_version(experience_id, requested_by):
    experience = Experience.query.get(experience_id)
    if not experience:
        raise PublishingError("experience not found")
    require_manage_permission(experience, requested_by)
    draft = _draft_for_edit(experience)
    if draft:
        return draft
    max_number = db.session.query(db.func.max(ExperienceVersion.version_number)).filter_by(experience_id=experience.id).scalar() or 0
    draft = ExperienceVersion(
        experience_id=experience.id,
        version_number=int(max_number) + 1,
        status="draft",
        created_by_user_id=requested_by,
        source_version_id=experience.current_published_version_id,
        public_destination=permanent_destination(experience),
    )
    db.session.add(draft)
    db.session.flush()
    _event("draft_created", experience, draft, requested_by, "Draft Version created")
    if experience.current_published_version_id:
        source = ExperienceVersion.query.get(experience.current_published_version_id)
        for item in source.trigger_snapshots:
            db.session.add(
                ExperienceVersionTrigger(
                    experience_version_id=draft.id,
                    trigger_id=item.trigger_id,
                    inclusion_order=item.inclusion_order,
                    is_active=item.is_active,
                    is_excluded=item.is_excluded,
                    reference_image_asset_id=item.reference_image_asset_id,
                    video_asset_id=item.video_asset_id,
                    recognition_artifact_id=item.recognition_artifact_id,
                    fallback_asset_id=item.fallback_asset_id,
                    creator_label=item.creator_label,
                    processing_snapshot_json=item.processing_snapshot_json,
                    source_revision_hash=item.source_revision_hash,
                )
            )
    db.session.commit()
    return draft


def _snapshot_payload(trigger):
    image = _asset_for(trigger, "reference_image")
    video = _asset_for(trigger, "video")
    recognition = _recognition_for(trigger)
    jobs_ok, job_statuses = _required_jobs_succeeded(trigger)
    payload = {
        "trigger_id": trigger.id,
        "name": trigger.name,
        "status": trigger.status,
        "is_active": bool(trigger.is_active),
        "is_excluded": bool(trigger.is_excluded),
        "reference_image_asset_id": image.id if image else None,
        "video_asset_id": video.id if video else None,
        "recognition_artifact_id": recognition.id if recognition else None,
        "jobs_ok": jobs_ok,
        "job_statuses": job_statuses,
    }
    encoded = json.dumps(payload, sort_keys=True)
    payload["source_revision_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return payload


def sync_version_snapshot_from_current_triggers(version):
    if version.is_immutable:
        raise PublishingError("published version snapshot is immutable")
    ExperienceVersionTrigger.query.filter_by(experience_version_id=version.id).delete()
    triggers = Trigger.query.filter_by(experience_id=version.experience_id).order_by(Trigger.created_at.asc(), Trigger.id.asc()).all()
    for index, trigger in enumerate(triggers, 1):
        payload = _snapshot_payload(trigger)
        db.session.add(
            ExperienceVersionTrigger(
                experience_version_id=version.id,
                trigger_id=trigger.id,
                inclusion_order=index,
                is_active=payload["is_active"],
                is_excluded=payload["is_excluded"],
                reference_image_asset_id=payload["reference_image_asset_id"],
                video_asset_id=payload["video_asset_id"],
                recognition_artifact_id=payload["recognition_artifact_id"],
                creator_label=payload["name"],
                processing_snapshot_json=json.dumps(payload, sort_keys=True),
                source_revision_hash=payload["source_revision_hash"],
            )
        )
    db.session.flush()
    return version.trigger_snapshots


def version_checksum(version):
    rows = [
        {
            "trigger_id": item.trigger_id,
            "order": item.inclusion_order,
            "active": item.is_active,
            "excluded": item.is_excluded,
            "image": item.reference_image_asset_id,
            "video": item.video_asset_id,
            "recognition": item.recognition_artifact_id,
            "hash": item.source_revision_hash,
        }
        for item in sorted(version.trigger_snapshots, key=lambda row: row.inclusion_order)
    ]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def evaluate_publish_readiness(experience_id, version_id=None, requested_by=None):
    experience = Experience.query.get(experience_id)
    if not experience:
        return {"ready": False, "issues": ["experience_not_found"], "warnings": []}
    if requested_by is not None:
        require_manage_permission(experience, requested_by)
    version = ExperienceVersion.query.get(version_id) if version_id else _draft_for_edit(experience)
    if not version:
        return {"ready": False, "issues": ["draft_version_missing"], "warnings": []}
    if version.status not in {"draft", "needs_attention", "ready_to_publish"}:
        return {"ready": False, "issues": ["version_not_draft"], "warnings": []}
    sync_version_snapshot_from_current_triggers(version)
    issues = []
    if experience.status in {"paused", "archived"}:
        issues.append(f"experience_{experience.status}")
    active = [item for item in version.trigger_snapshots if item.is_active and not item.is_excluded]
    if not active:
        issues.append("no_active_included_triggers")
    for item in active:
        if not item.reference_image_asset_id:
            issues.append(f"trigger_{item.trigger_id}_missing_image")
        if not item.video_asset_id:
            issues.append(f"trigger_{item.trigger_id}_missing_video")
        if not item.recognition_artifact_id:
            issues.append(f"trigger_{item.trigger_id}_missing_recognition")
        payload = json.loads(item.processing_snapshot_json or "{}")
        if payload.get("status") == "failed":
            issues.append(f"trigger_{item.trigger_id}_failed")
        if not payload.get("jobs_ok"):
            issues.append(f"trigger_{item.trigger_id}_processing_incomplete")
    if not experience.public_key:
        issues.append("public_identity_missing")
    if not permanent_destination(experience):
        issues.append("qr_destination_missing")
    processing = ProcessingJob.query.filter(
        ProcessingJob.experience_id == experience.id,
        ProcessingJob.status.in_(PROCESSING_STATUSES),
    ).first()
    if processing:
        issues.append("processing_in_progress")
    checksum = version_checksum(version)
    version.publication_checksum = checksum
    version.status = "ready_to_publish" if not issues else "needs_attention"
    version.processing_snapshot_json = json.dumps({"issues": issues}, sort_keys=True)
    db.session.commit()
    return {"ready": not issues, "issues": issues, "warnings": [], "version_id": version.id, "checksum": checksum}


def publish_experience_version(experience_id, version_id, requested_by, idempotency_key, expected_checksum=None):
    if not experience_publishing_enabled():
        raise PublishingError("publishing disabled")
    if not idempotency_key or len(str(idempotency_key)) > 128:
        raise PublishingError("valid idempotency key required")
    experience = Experience.query.get(experience_id)
    if not experience:
        raise PublishingError("experience not found")
    require_publish_permission(experience, requested_by)
    version = ExperienceVersion.query.filter_by(id=version_id, experience_id=experience.id).first()
    if not version:
        raise PublishingError("version not found")
    existing = ExperienceVersion.query.filter_by(experience_id=experience.id, publication_idempotency_key=idempotency_key, status="published").first()
    if existing:
        return {"published": True, "version": existing, "idempotent": True}
    _event("publish_requested", experience, version, requested_by, "Publish requested", {"idempotency_key": idempotency_key})
    try:
        readiness = evaluate_publish_readiness(experience.id, version.id, requested_by=requested_by)
        if not readiness["ready"]:
            _event("publish_validation_failed", experience, version, requested_by, "Publish validation failed", {"issues": readiness["issues"]})
            db.session.commit()
            raise PublishingError(";".join(readiness["issues"]))
        if expected_checksum and expected_checksum != readiness["checksum"]:
            raise PublishingError("stale draft checksum")
        previous_id = experience.current_published_version_id
        version.status = "publishing"
        _event("publication_started", experience, version, requested_by, "Publication started")
        db.session.flush()
        previous = ExperienceVersion.query.get(previous_id) if previous_id else None
        if previous and previous.id != version.id:
            previous.status = "superseded"
            previous.superseded_at = get_utc_now()
            _event("version_superseded", experience, previous, requested_by, "Previous Version superseded", {"new_version_id": version.id})
        version.status = "published"
        version.published_at = get_utc_now()
        version.published_by_user_id = requested_by
        version.publication_idempotency_key = idempotency_key
        version.publication_checksum = readiness["checksum"]
        version.public_destination = permanent_destination(experience)
        version.is_immutable = True
        experience.current_published_version_id = version.id
        experience.status = "ready_to_publish"
        _event("publication_succeeded", experience, version, requested_by, "Publication succeeded", {"previous_version_id": previous_id})
        db.session.commit()
        return {"published": True, "version": version, "idempotent": False}
    except Exception:
        db.session.rollback()
        fresh = Experience.query.get(experience_id)
        _event("publication_failed", fresh, version if version.id else None, requested_by, "Publication failed")
        db.session.commit()
        raise


def rollback_experience_to_version(experience_id, target_version_id, requested_by, idempotency_key=None):
    if not version_rollback_enabled():
        raise PublishingError("rollback disabled")
    experience = Experience.query.get(experience_id)
    if not experience:
        raise PublishingError("experience not found")
    require_publish_permission(experience, requested_by)
    target = ExperienceVersion.query.filter_by(id=target_version_id, experience_id=experience.id).first()
    if not target or not target.is_immutable or target.status not in {"published", "superseded"}:
        raise PublishingError("target version is not rollbackable")
    if not target.trigger_snapshots:
        raise PublishingError("target snapshot missing")
    for item in target.trigger_snapshots:
        if item.is_active and not item.is_excluded:
            if not item.video_asset_id or not Asset.query.get(item.video_asset_id):
                raise PublishingError("historical video missing")
            if not item.reference_image_asset_id or not Asset.query.get(item.reference_image_asset_id):
                raise PublishingError("historical image missing")
    if experience.current_published_version_id == target.id:
        return {"rolled_back": True, "version": target, "idempotent": True}
    current = ExperienceVersion.query.get(experience.current_published_version_id) if experience.current_published_version_id else None
    _event("rollback_requested", experience, target, requested_by, "Rollback requested", {"current_version_id": current.id if current else None})
    if current:
        current.status = "superseded"
        current.superseded_at = get_utc_now()
    target.status = "published"
    target.rollback_source_version_id = current.id if current else None
    experience.current_published_version_id = target.id
    _event("rollback_succeeded", experience, target, requested_by, "Rollback succeeded")
    db.session.commit()
    return {"rolled_back": True, "version": target, "idempotent": False}


def set_experience_public_state(experience_id, state, requested_by):
    if not experience_pause_enabled():
        raise PublishingError("pause/archive disabled")
    if state not in {"paused", "ready_to_publish", "archived"}:
        raise PublishingError("invalid public state")
    experience = Experience.query.get(experience_id)
    if not experience:
        raise PublishingError("experience not found")
    require_publish_permission(experience, requested_by)
    event_type = {"paused": "experience_paused", "ready_to_publish": "experience_resumed", "archived": "experience_archived"}[state]
    experience.status = state
    _event(event_type, experience, ExperienceVersion.query.get(experience.current_published_version_id), requested_by, event_type.replace("_", " ").title())
    db.session.commit()
    return experience


def resolve_published_experience(public_key):
    if not public_experience_route_enabled():
        return {"status": "disabled", "http_status": 404, "message": "Experience publishing is unavailable."}
    experience = Experience.query.filter_by(public_key=public_key).first()
    if not experience:
        return {"status": "unknown", "http_status": 404, "message": "Experience not found."}
    if experience.status == "paused":
        return {"status": "paused", "http_status": 503, "message": "This Experience is paused.", "experience": experience}
    if experience.status == "archived":
        return {"status": "archived", "http_status": 410, "message": "This Experience is archived.", "experience": experience}
    if not experience.current_published_version_id:
        return {"status": "unpublished", "http_status": 404, "message": "This Experience is not published yet.", "experience": experience}
    version = ExperienceVersion.query.get(experience.current_published_version_id)
    if not version or version.status != "published":
        return {"status": "unavailable", "http_status": 503, "message": "This Experience is temporarily unavailable.", "experience": experience}
    snapshots = (
        ExperienceVersionTrigger.query.filter_by(experience_version_id=version.id, is_active=True, is_excluded=False)
        .order_by(ExperienceVersionTrigger.inclusion_order.asc())
        .limit(100)
        .all()
    )
    return {
        "status": "published",
        "http_status": 200,
        "message": "Experience ready.",
        "experience": experience,
        "version": version,
        "snapshots": snapshots,
        "destination": permanent_destination(experience),
    }
