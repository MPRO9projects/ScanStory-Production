import os
import tempfile
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import case, func
from werkzeug.utils import secure_filename

from feature_flags import (
    experience_creator_enabled,
    experience_pause_enabled,
    experience_publishing_enabled,
    experience_qr_asset_enabled,
    processing_status_ui_enabled,
    trigger_management_enabled,
    version_rollback_enabled,
)
from models import Asset, Experience, ExperienceVersion, ProcessingEvent, ProcessingJob, Trigger, TriggerAsset, User, Workspace, WorkspaceMember, db
from public_keys import generate_unique_public_key
from storage import LocalFilesystemStorage, build_storage_key


experience_creator_bp = Blueprint("experience_creator", __name__)

MANAGE_ROLES = {"owner", "admin", "creator"}
READ_ROLES = MANAGE_ROLES | {"reviewer", "publisher", "analyst"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed_terminal", "cancelled"}


def _current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


def _gate_enabled():
    if not experience_creator_enabled():
        abort(404)


def _require_user():
    _gate_enabled()
    user = _current_user()
    if not user:
        return None, redirect(url_for("login"))
    return user, None


def _member_for(user, workspace_id, roles=READ_ROLES):
    return WorkspaceMember.query.filter(
        WorkspaceMember.user_id == user.id,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.status == "active",
        WorkspaceMember.role.in_(roles),
    ).first()


def _ensure_workspace(user):
    member = WorkspaceMember.query.filter(
        WorkspaceMember.user_id == user.id,
        WorkspaceMember.status == "active",
        WorkspaceMember.role.in_(MANAGE_ROLES),
    ).order_by(WorkspaceMember.id.asc()).first()
    if member:
        return member.workspace
    workspace = Workspace(
        public_key=generate_unique_public_key(db.session, Workspace, "wsp"),
        name=f"{user.full_name or user.email} Workspace",
        workspace_type="personal",
        status="active",
    )
    db.session.add(workspace)
    db.session.flush()
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner", status="active"))
    db.session.commit()
    return workspace


def _experience_for_user(user, experience_id, roles=READ_ROLES):
    experience = Experience.query.get_or_404(experience_id)
    if not _member_for(user, experience.workspace_id, roles):
        abort(403)
    return experience


def _trigger_for_user(user, experience_id, trigger_id, roles=MANAGE_ROLES):
    if roles == MANAGE_ROLES and not trigger_management_enabled():
        abort(404)
    experience = _experience_for_user(user, experience_id, roles)
    trigger = Trigger.query.filter_by(id=trigger_id, experience_id=experience.id).first_or_404()
    return experience, trigger


def _storage():
    root = os.environ.get("SCANSTORY_EXPERIENCE_STORAGE_ROOT") or os.path.join(os.getcwd(), "data", "experience_creator")
    return LocalFilesystemStorage(root)


def _save_upload(workspace, experience, trigger, upload, role):
    filename = secure_filename(upload.filename or f"{role}.bin") or f"{role}.bin"
    asset_type = "image" if role == "reference_image" else "video"
    key = build_storage_key(workspace.public_key, experience.public_key, trigger.public_key, role, filename)
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        temp_name = tmp.name
        upload.save(tmp)
    try:
        _storage().put_file(key, temp_name)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    asset = Asset(
        public_key=generate_unique_public_key(db.session, Asset, "ast"),
        workspace_id=workspace.id,
        asset_type=asset_type,
        storage_provider="local_legacy",
        storage_key=key,
        original_filename=filename,
        mime_type=upload.mimetype,
        size_bytes=_storage().get_metadata(key)["size"],
    )
    db.session.add(asset)
    db.session.flush()
    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=asset.id, role=role))
    return asset


def _asset_for(trigger, role):
    link = TriggerAsset.query.filter_by(trigger_id=trigger.id, role=role).order_by(TriggerAsset.id.desc()).first()
    return link.asset if link else None


def _status_counts_query():
    ready = func.sum(case((Trigger.status == "ready", 1), else_=0)).label("ready_count")
    excluded = func.sum(case((Trigger.is_excluded.is_(True), 1), else_=0)).label("excluded_count")
    needs_attention = func.sum(case((Trigger.status == "failed", 1), else_=0)).label("needs_attention_count")
    processing = func.sum(
        case((Trigger.status.in_(["uploading", "validating", "optimizing", "extracting", "robustness_testing", "retry_scheduled", "retrying"]), 1), else_=0)
    ).label("processing_count")
    return ready, excluded, needs_attention, processing


def _experience_destination(experience):
    return f"/experiences/{experience.public_key}/processing-ready-disabled"


def _event(event_type, experience=None, trigger=None, actor=None, message=None):
    db.session.add(
        ProcessingEvent(
            workspace_id=experience.workspace_id if experience else trigger.experience.workspace_id,
            experience_id=experience.id if experience else trigger.experience_id,
            trigger_id=trigger.id if trigger else None,
            event_type=event_type,
            actor=actor,
            creator_message=message,
        )
    )


@experience_creator_bp.route("/experiences")
def experience_list():
    user, response = _require_user()
    if response:
        return response
    workspace_ids = [
        row.workspace_id
        for row in WorkspaceMember.query.filter(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.status == "active",
            WorkspaceMember.role.in_(READ_ROLES),
        ).all()
    ]
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    sort = request.args.get("sort", "updated_desc")
    page = max(1, request.args.get("page", default=1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", default=20, type=int)))
    ready, excluded, needs_attention, processing = _status_counts_query()
    query = (
        db.session.query(Experience, func.count(Trigger.id).label("trigger_count"), ready, excluded, needs_attention, processing)
        .outerjoin(Trigger, Trigger.experience_id == Experience.id)
        .filter(Experience.workspace_id.in_(workspace_ids or [-1]))
        .group_by(Experience.id)
    )
    if search:
        query = query.filter(Experience.name.ilike(f"%{search}%"))
    if status:
        query = query.filter(Experience.status == status)
    if sort == "name":
        query = query.order_by(Experience.name.asc())
    elif sort == "created_asc":
        query = query.order_by(Experience.created_at.asc())
    else:
        query = query.order_by(Experience.updated_at.desc(), Experience.id.desc())
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    return render_template(
        "user/experiences/list.html",
        user=user,
        rows=rows,
        page=page,
        per_page=per_page,
        total=total,
        search=search,
        status=status,
        sort=sort,
        qr_enabled=experience_qr_asset_enabled(),
    )


@experience_creator_bp.route("/experiences/new", methods=["GET", "POST"])
def experience_new():
    user, response = _require_user()
    if response:
        return response
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        if not name or len(name) > 255:
            return render_template("user/experiences/new.html", user=user, error="Enter an Experience name under 255 characters.", name=name, description=description), 400
        if not user.has_active_subscription():
            flash("Your current plan must be active to create an Experience.", "error")
            return redirect(url_for("subscribe_page"))
        workspace = _ensure_workspace(user)
        experience = Experience(
            public_key=generate_unique_public_key(db.session, Experience, "exp"),
            workspace_id=workspace.id,
            name=name,
            description=description,
            status="draft",
            created_by_user_id=user.id,
        )
        db.session.add(experience)
        db.session.flush()
        _event("experience_created", experience=experience, actor=str(user.id), message="Experience created")
        db.session.commit()
        return redirect(url_for("experience_creator.experience_detail", experience_id=experience.id))
    return render_template("user/experiences/new.html", user=user, error=None, name="", description="")


@experience_creator_bp.route("/experiences/<int:experience_id>")
def experience_detail(experience_id):
    user, response = _require_user()
    if response:
        return response
    experience = _experience_for_user(user, experience_id)
    triggers = Trigger.query.filter_by(experience_id=experience.id).order_by(Trigger.created_at.asc(), Trigger.id.asc()).limit(100).all()
    events = (
        ProcessingEvent.query.filter_by(experience_id=experience.id)
        .order_by(ProcessingEvent.created_at.desc(), ProcessingEvent.id.desc())
        .limit(50)
        .all()
    )
    versions = ExperienceVersion.query.filter_by(experience_id=experience.id).order_by(ExperienceVersion.version_number.desc()).limit(50).all()
    readiness = None
    if experience_publishing_enabled():
        try:
            from publishing import ensure_draft_version, evaluate_publish_readiness

            draft = ensure_draft_version(experience.id, requested_by=user.id)
            readiness = evaluate_publish_readiness(experience.id, draft.id, requested_by=user.id)
        except PermissionError:
            readiness = {"ready": False, "issues": ["publish_permission_required"], "warnings": []}
    statuses = {}
    if processing_status_ui_enabled():
        from processing_orchestration import get_trigger_processing_status, get_experience_processing_status

        statuses["experience"] = get_experience_processing_status(experience.id, user_id=user.id)
        statuses["triggers"] = {trigger.id: get_trigger_processing_status(trigger.id, user_id=user.id) for trigger in triggers}
    return render_template(
        "user/experiences/detail.html",
        user=user,
        experience=experience,
        triggers=triggers,
        events=events,
        statuses=statuses,
        versions=versions,
        readiness=readiness,
        trigger_management=trigger_management_enabled(),
        status_ui=processing_status_ui_enabled(),
        qr_enabled=experience_qr_asset_enabled(),
        publishing_enabled=experience_publishing_enabled(),
        rollback_enabled=version_rollback_enabled(),
        pause_enabled=experience_pause_enabled(),
        destination=_experience_destination(experience),
        asset_for=_asset_for,
    )


@experience_creator_bp.post("/experiences/<int:experience_id>/versions/draft")
def create_draft_version(experience_id):
    user, response = _require_user()
    if response:
        return response
    if not experience_publishing_enabled():
        abort(404)
    from publishing import ensure_draft_version

    ensure_draft_version(experience_id, requested_by=user.id)
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/publish")
def publish_experience(experience_id):
    user, response = _require_user()
    if response:
        return response
    if not experience_publishing_enabled():
        abort(404)
    from publishing import PublishingError, ensure_draft_version, evaluate_publish_readiness, publish_experience_version

    try:
        draft = ensure_draft_version(experience_id, requested_by=user.id)
        readiness = evaluate_publish_readiness(experience_id, draft.id, requested_by=user.id)
        publish_experience_version(
            experience_id,
            draft.id,
            requested_by=user.id,
            idempotency_key=request.form.get("idempotency_key") or f"publish-{experience_id}-{draft.id}",
            expected_checksum=readiness.get("checksum"),
        )
        flash("Experience published.", "success")
    except (PublishingError, PermissionError) as exc:
        flash(f"Publish blocked: {exc}", "error")
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/versions/<int:version_id>/rollback")
def rollback_experience(experience_id, version_id):
    user, response = _require_user()
    if response:
        return response
    if not version_rollback_enabled():
        abort(404)
    from publishing import PublishingError, rollback_experience_to_version

    try:
        rollback_experience_to_version(experience_id, version_id, requested_by=user.id, idempotency_key=request.form.get("idempotency_key"))
        flash("Experience rolled back.", "success")
    except (PublishingError, PermissionError) as exc:
        flash(f"Rollback blocked: {exc}", "error")
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/pause")
def pause_experience(experience_id):
    return _set_public_state(experience_id, "paused")


@experience_creator_bp.post("/experiences/<int:experience_id>/resume")
def resume_experience(experience_id):
    return _set_public_state(experience_id, "ready_to_publish")


@experience_creator_bp.post("/experiences/<int:experience_id>/archive")
def archive_experience(experience_id):
    return _set_public_state(experience_id, "archived")


def _set_public_state(experience_id, state):
    user, response = _require_user()
    if response:
        return response
    if not experience_pause_enabled():
        abort(404)
    from publishing import PublishingError, set_experience_public_state

    try:
        set_experience_public_state(experience_id, state, requested_by=user.id)
    except (PublishingError, PermissionError) as exc:
        flash(f"State change blocked: {exc}", "error")
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.route("/e/<public_key>")
def public_experience(public_key):
    from publishing import resolve_published_experience
    from scanner_runtime import create_viewer_session_id

    result = resolve_published_experience(public_key)
    if result["status"] == "published" and not session.get("experience_viewer_session_id"):
        session["experience_viewer_session_id"] = create_viewer_session_id()
    result["viewer_session_id"] = session.get("experience_viewer_session_id")
    if result["status"] != "published":
        return render_template("user/experiences/public_unavailable.html", result=result), result["http_status"]
    return render_template("user/experiences/public_viewer.html", result=result), 200


@experience_creator_bp.route("/experiences/<int:experience_id>/triggers/new", methods=["GET", "POST"])
def trigger_new(experience_id):
    user, response = _require_user()
    if response:
        return response
    if not trigger_management_enabled():
        abort(404)
    experience = _experience_for_user(user, experience_id, MANAGE_ROLES)
    if request.method == "POST":
        images = [item for item in request.files.getlist("reference_images") + request.files.getlist("images") if item and item.filename]
        videos = [item for item in request.files.getlist("videos") if item and item.filename]
        labels = request.form.getlist("labels")
        if not images or not videos or len(images) != len(videos):
            return render_template("user/experiences/trigger_new.html", user=user, experience=experience, error="Upload matching image and video counts."), 400
        created = []
        workspace = experience.workspace
        for index, (image, video) in enumerate(zip(images, videos), 1):
            trigger = Trigger(
                public_key=generate_unique_public_key(db.session, Trigger, "trg"),
                experience_id=experience.id,
                name=(labels[index - 1].strip() if index - 1 < len(labels) and labels[index - 1].strip() else f"Trigger {index}"),
                status="uploading",
            )
            db.session.add(trigger)
            db.session.flush()
            _save_upload(workspace, experience, trigger, image, "reference_image")
            _save_upload(workspace, experience, trigger, video, "video")
            trigger.status = "draft"
            _event("trigger_created", trigger=trigger, actor=str(user.id), message="Trigger uploaded")
            created.append(trigger)
        db.session.commit()
        from processing_orchestration import orchestrate_trigger_processing

        for trigger in created:
            orchestrate_trigger_processing(trigger.id, requested_by=str(user.id))
        flash(f"{len(created)} Trigger upload queued for processing.", "success")
        return redirect(url_for("experience_creator.experience_detail", experience_id=experience.id))
    return render_template("user/experiences/trigger_new.html", user=user, experience=experience, error=None)


@experience_creator_bp.route("/experiences/<int:experience_id>/processing-status")
def processing_status(experience_id):
    user, response = _require_user()
    if response:
        return response
    if not processing_status_ui_enabled():
        abort(404)
    experience = _experience_for_user(user, experience_id)
    from processing_orchestration import get_experience_processing_status, get_trigger_processing_status

    triggers = Trigger.query.filter_by(experience_id=experience.id).order_by(Trigger.id.asc()).limit(100).all()
    return jsonify(
        {
            "experience": get_experience_processing_status(experience.id, user_id=user.id),
            "triggers": [get_trigger_processing_status(trigger.id, user_id=user.id) for trigger in triggers],
            "history_limit": 50,
        }
    )


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/retry")
def retry_trigger(experience_id, trigger_id):
    user, response = _require_user()
    if response:
        return response
    _trigger_for_user(user, experience_id, trigger_id)
    from processing_orchestration import retry_failed_trigger_processing

    retry_failed_trigger_processing(trigger_id, requested_by=str(user.id))
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/replace-image")
def replace_image(experience_id, trigger_id):
    return _replace_asset(experience_id, trigger_id, "reference_image", "replacement_image")


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/replace-video")
def replace_video(experience_id, trigger_id):
    return _replace_asset(experience_id, trigger_id, "video", "replacement_video")


def _replace_asset(experience_id, trigger_id, role, field):
    user, response = _require_user()
    if response:
        return response
    experience, trigger = _trigger_for_user(user, experience_id, trigger_id)
    upload = request.files.get(field) or request.files.get(role)
    if not upload or not upload.filename:
        flash("Choose a replacement file.", "error")
        return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))
    asset = _asset_for(trigger, role)
    if not asset:
        asset = _save_upload(experience.workspace, experience, trigger, upload, role)
        db.session.commit()
    else:
        filename = secure_filename(upload.filename or asset.original_filename or f"{role}.bin")
        key = build_storage_key(experience.workspace.public_key, experience.public_key, trigger.public_key, role, filename)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            temp_name = tmp.name
            upload.save(tmp)
        try:
            _storage().put_file(key, temp_name)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        asset.original_filename = filename
        asset.mime_type = upload.mimetype
        asset.size_bytes = _storage().get_metadata(key)["size"]
        db.session.commit()
    from processing_orchestration import record_source_replaced

    record_source_replaced(trigger.id, role, key if "key" in locals() else asset.storage_key, requested_by=str(user.id))
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/regenerate-recognition")
def regenerate_recognition(experience_id, trigger_id):
    user, response = _require_user()
    if response:
        return response
    _trigger_for_user(user, experience_id, trigger_id)
    from processing_orchestration import regenerate_recognition_for_trigger

    regenerate_recognition_for_trigger(trigger_id, requested_by=str(user.id))
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/regenerate-qr")
def regenerate_qr(experience_id):
    user, response = _require_user()
    if response:
        return response
    if not experience_qr_asset_enabled():
        abort(404)
    experience = _experience_for_user(user, experience_id, MANAGE_ROLES)
    from processing_orchestration import regenerate_qr_for_experience

    regenerate_qr_for_experience(experience.id, _experience_destination(experience), requested_by=str(user.id))
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience_id))


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/exclude")
def exclude_trigger(experience_id, trigger_id):
    user, response = _require_user()
    if response:
        return response
    experience, trigger = _trigger_for_user(user, experience_id, trigger_id)
    trigger.is_excluded = True
    trigger.status = "excluded"
    _event("trigger_excluded", trigger=trigger, actor=str(user.id), message="Trigger excluded")
    db.session.commit()
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience.id))


@experience_creator_bp.post("/experiences/<int:experience_id>/triggers/<int:trigger_id>/reactivate")
def reactivate_trigger(experience_id, trigger_id):
    user, response = _require_user()
    if response:
        return response
    experience, trigger = _trigger_for_user(user, experience_id, trigger_id)
    trigger.is_excluded = False
    trigger.status = "draft"
    _event("trigger_reactivated", trigger=trigger, actor=str(user.id), message="Trigger reactivated")
    db.session.commit()
    from processing_orchestration import orchestrate_trigger_processing

    orchestrate_trigger_processing(trigger.id, reason="reactivated", requested_by=str(user.id))
    return redirect(url_for("experience_creator.experience_detail", experience_id=experience.id))
