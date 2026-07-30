from dataclasses import dataclass, field
from datetime import datetime

from public_keys import generate_unique_public_key
from models import (
    Admin,
    Asset,
    Experience,
    MigrationCheckpoint,
    ProcessingJob,
    Project,
    ProjectPair,
    RecognitionArtifact,
    Trigger,
    TriggerAsset,
    User,
    Workspace,
    WorkspaceMember,
    db,
)


MIGRATION_NAME = "gate_c_compatibility_model"


@dataclass
class MigrationResult:
    created: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    dry_run: bool = False

    def inc(self, bucket, key, amount=1):
        target = getattr(self, bucket)
        target[key] = target.get(key, 0) + amount


def default_workspace_name(user):
    if user.full_name:
        label = user.full_name
    elif user.email:
        label = user.email.split("@")[0]
    else:
        label = f"User {user.id}"
    return f"{label}'s Workspace"


def _checkpoint(entity_type, legacy_id, target_id, status, error_message=None):
    checkpoint = MigrationCheckpoint.query.filter_by(
        migration_name=MIGRATION_NAME,
        entity_type=entity_type,
        legacy_id=legacy_id,
    ).first()
    if not checkpoint:
        checkpoint = MigrationCheckpoint(
            migration_name=MIGRATION_NAME,
            entity_type=entity_type,
            legacy_id=legacy_id,
        )
        db.session.add(checkpoint)
    checkpoint.target_id = target_id
    checkpoint.status = status
    checkpoint.error_message = error_message
    checkpoint.attempt_count = (checkpoint.attempt_count or 0) + 1
    checkpoint.completed_at = datetime.utcnow() if status == "completed" else None
    return checkpoint


def _first_owner_workspace(user_id):
    membership = (
        WorkspaceMember.query.join(Workspace)
        .filter(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.role == "owner",
            WorkspaceMember.status == "active",
            Workspace.workspace_type == "personal",
        )
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    return membership.workspace if membership else None


def backfill_default_workspaces(dry_run=True):
    result = MigrationResult(dry_run=dry_run)
    for user in User.query.order_by(User.id.asc()).all():
        if not user.email:
            result.errors.append({"entity": "user", "legacy_id": user.id, "error": "missing email"})
            result.inc("skipped", "users")
            if not dry_run:
                _checkpoint("user", user.id, None, "failed", "missing email")
            continue

        workspace = _first_owner_workspace(user.id)
        if workspace:
            result.inc("existing", "workspaces")
            if not dry_run:
                _checkpoint("user", user.id, workspace.id, "completed")
            continue

        if dry_run:
            result.inc("created", "workspaces")
            result.inc("created", "workspace_members")
            continue

        workspace = Workspace(
            public_key=generate_unique_public_key(db.session, Workspace, "wsp"),
            name=default_workspace_name(user),
            workspace_type="personal",
            status="active",
        )
        db.session.add(workspace)
        db.session.flush()
        member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner", status="active")
        db.session.add(member)
        db.session.flush()
        _checkpoint("user", user.id, workspace.id, "completed")
        result.inc("created", "workspaces")
        result.inc("created", "workspace_members")
    if not dry_run:
        db.session.commit()
    return result


def _admin_resolution_for(project, ownership_resolutions):
    if not ownership_resolutions:
        return None
    return ownership_resolutions.get(project.id) or ownership_resolutions.get(str(project.id))


def map_projects_to_experiences(dry_run=True, ownership_resolutions=None):
    result = MigrationResult(dry_run=dry_run)
    for project in Project.query.order_by(Project.id.asc()).all():
        existing = Experience.query.filter_by(legacy_project_id=project.id).first()
        if existing:
            result.inc("existing", "experiences")
            if not dry_run:
                _checkpoint("project", project.id, existing.id, "completed")
            continue

        target_workspace = None
        created_by_user_id = project.owner_user_id

        if project.owner_admin_id and not project.owner_user_id:
            resolution = _admin_resolution_for(project, ownership_resolutions)
            if not resolution:
                message = "admin-owned project requires explicit workspace resolution"
                result.errors.append({"entity": "project", "legacy_id": project.id, "error": message})
                result.inc("skipped", "projects")
                if not dry_run:
                    _checkpoint("project", project.id, None, "failed", message)
                continue
            target_workspace = Workspace.query.get(resolution["target_workspace_id"])
            if not target_workspace:
                message = "ownership mapping references unknown workspace"
                result.errors.append({"entity": "project", "legacy_id": project.id, "error": message})
                result.inc("skipped", "projects")
                if not dry_run:
                    _checkpoint("project", project.id, None, "failed", message)
                continue

        elif not project.owner_user_id:
            message = "project has no user owner"
            result.errors.append({"entity": "project", "legacy_id": project.id, "error": message})
            result.inc("skipped", "projects")
            if not dry_run:
                _checkpoint("project", project.id, None, "failed", message)
            continue

        else:
            target_workspace = _first_owner_workspace(project.owner_user_id)
            if not target_workspace and not dry_run:
                message = "owner has no personal workspace"
                result.errors.append({"entity": "project", "legacy_id": project.id, "error": message})
                result.inc("skipped", "projects")
                _checkpoint("project", project.id, None, "failed", message)
                continue

        if dry_run:
            result.inc("created", "experiences")
            continue

        experience = Experience(
            public_key=generate_unique_public_key(db.session, Experience, "exp"),
            workspace_id=target_workspace.id,
            legacy_project_id=project.id,
            name=project.name,
            description=project.description,
            status="draft",
            created_by_user_id=created_by_user_id,
        )
        db.session.add(experience)
        db.session.flush()
        _checkpoint("project", project.id, experience.id, "completed")
        result.inc("created", "experiences")
    if not dry_run:
        db.session.commit()
    return result


def _asset_for(workspace_id, asset_type, storage_key, original_filename):
    asset = Asset.query.filter_by(workspace_id=workspace_id, asset_type=asset_type, storage_key=storage_key).first()
    if asset:
        return asset, False
    asset = Asset(
        public_key=generate_unique_public_key(db.session, Asset, "ast"),
        workspace_id=workspace_id,
        asset_type=asset_type,
        storage_provider="local_legacy",
        storage_key=storage_key,
        original_filename=original_filename,
        status="available",
    )
    db.session.add(asset)
    db.session.flush()
    return asset, True


def _attach_asset(trigger, asset, role):
    link = TriggerAsset.query.filter_by(trigger_id=trigger.id, asset_id=asset.id, role=role).first()
    if link:
        return False
    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=asset.id, role=role))
    return True


def map_pairs_to_triggers(dry_run=True, media_exists=None):
    result = MigrationResult(dry_run=dry_run)
    for pair in ProjectPair.query.order_by(ProjectPair.project_id.asc(), ProjectPair.pair_index.asc()).all():
        existing = Trigger.query.filter_by(legacy_project_pair_id=pair.id).first()
        if existing:
            result.inc("existing", "triggers")
            if not dry_run:
                _checkpoint("project_pair", pair.id, existing.id, "completed")
            continue

        experience = Experience.query.filter_by(legacy_project_id=pair.project_id).first()
        if not experience:
            if dry_run:
                project = Project.query.get(pair.project_id)
                if project and project.owner_user_id and not project.owner_admin_id:
                    experience = True
                else:
                    message = "missing mapped experience"
                    result.errors.append({"entity": "project_pair", "legacy_id": pair.id, "error": message})
                    result.inc("skipped", "project_pairs")
                    continue
            else:
                message = "missing mapped experience"
                result.errors.append({"entity": "project_pair", "legacy_id": pair.id, "error": message})
                result.inc("skipped", "project_pairs")
                _checkpoint("project_pair", pair.id, None, "failed", message)
                continue

        if experience is True:
            workspace_id = None
        else:
            workspace_id = experience.workspace_id

        if not experience:
            message = "missing mapped experience"
            result.errors.append({"entity": "project_pair", "legacy_id": pair.id, "error": message})
            result.inc("skipped", "project_pairs")
            if not dry_run:
                _checkpoint("project_pair", pair.id, None, "failed", message)
            continue

        warnings = []
        for kind, filename in (("image", pair.image_filename), ("video", pair.video_filename)):
            if not filename:
                warnings.append(f"missing {kind} filename")
            elif media_exists and not media_exists(kind, pair):
                warnings.append(f"missing {kind} file")
        if media_exists and not media_exists("feature_npz", pair):
            warnings.append("missing feature artifact")

        if dry_run:
            result.inc("created", "triggers")
            result.inc("created", "assets", 2)
            result.inc("created", "recognition_artifacts")
            if warnings:
                result.errors.append({"entity": "project_pair", "legacy_id": pair.id, "error": "; ".join(warnings)})
            continue

        trigger = Trigger(
            public_key=generate_unique_public_key(db.session, Trigger, "trg"),
            experience_id=experience.id,
            legacy_project_pair_id=pair.id,
            name=f"Pair {pair.pair_index}",
            trigger_type="image_marker",
            status="ready" if pair.is_ready_for_detection else "draft",
            is_active=True,
            is_excluded=False,
        )
        db.session.add(trigger)
        db.session.flush()

        image_asset, image_created = _asset_for(workspace_id, "image", pair.image_filename, pair.original_image_name)
        video_asset, video_created = _asset_for(workspace_id, "video", pair.video_filename, pair.original_video_name)
        _attach_asset(trigger, image_asset, "reference_image")
        _attach_asset(trigger, video_asset, "video")
        artifact = RecognitionArtifact(
            trigger_id=trigger.id,
            artifact_type="feature_npz",
            algorithm="orb",
            algorithm_version="gate-c-legacy",
            storage_provider="local_legacy",
            storage_key=f"{pair.project_id}_{pair.pair_index}.npz",
            status="available" if "missing feature artifact" not in warnings else "missing",
        )
        db.session.add(artifact)
        _checkpoint("project_pair", pair.id, trigger.id, "completed", "; ".join(warnings) if warnings else None)
        result.inc("created", "triggers")
        result.inc("created", "assets", int(image_created) + int(video_created))
        result.inc("created", "recognition_artifacts")
        if warnings:
            result.errors.append({"entity": "project_pair", "legacy_id": pair.id, "error": "; ".join(warnings)})
    if not dry_run:
        db.session.commit()
    return result


def run_gate_c_migration(dry_run=True, media_exists=None, ownership_resolutions=None):
    result = MigrationResult(dry_run=dry_run)
    for step in (
        backfill_default_workspaces(dry_run=dry_run),
        map_projects_to_experiences(dry_run=dry_run, ownership_resolutions=ownership_resolutions),
        map_pairs_to_triggers(dry_run=dry_run, media_exists=media_exists),
    ):
        for bucket in ("created", "existing", "skipped"):
            for key, value in getattr(step, bucket).items():
                result.inc(bucket, key, value)
        result.errors.extend(step.errors)
    return result


def verify_gate_c_migration():
    users = User.query.count()
    projects = Project.query.count()
    pairs = ProjectPair.query.count()
    owner_memberships = WorkspaceMember.query.filter_by(role="owner", status="active").count()
    mapped_experiences = Experience.query.filter(Experience.legacy_project_id.isnot(None)).count()
    mapped_triggers = Trigger.query.filter(Trigger.legacy_project_pair_id.isnot(None)).count()
    duplicate_experience_mappings = (
        db.session.query(Experience.legacy_project_id)
        .filter(Experience.legacy_project_id.isnot(None))
        .group_by(Experience.legacy_project_id)
        .having(db.func.count(Experience.id) > 1)
        .count()
    )
    duplicate_trigger_mappings = (
        db.session.query(Trigger.legacy_project_pair_id)
        .filter(Trigger.legacy_project_pair_id.isnot(None))
        .group_by(Trigger.legacy_project_pair_id)
        .having(db.func.count(Trigger.id) > 1)
        .count()
    )
    checkpoint_failures = MigrationCheckpoint.query.filter_by(migration_name=MIGRATION_NAME, status="failed").count()
    orphan_experiences = Experience.query.filter(~Experience.workspace.has()).count()
    orphan_triggers = Trigger.query.filter(~Trigger.experience.has()).count()
    public_key_models = [Workspace, Experience, Trigger, Asset, ProcessingJob]
    duplicate_public_keys = 0
    for model in public_key_models:
        duplicate_public_keys += (
            db.session.query(model.public_key)
            .group_by(model.public_key)
            .having(db.func.count(model.id) > 1)
            .count()
        )
    return {
        "users": users,
        "workspaces": Workspace.query.count(),
        "workspace_owner_memberships": owner_memberships,
        "projects": projects,
        "mapped_experiences": mapped_experiences,
        "project_pairs": pairs,
        "mapped_triggers": mapped_triggers,
        "duplicate_experience_mappings": duplicate_experience_mappings,
        "duplicate_trigger_mappings": duplicate_trigger_mappings,
        "checkpoint_failures": checkpoint_failures,
        "orphan_experiences": orphan_experiences,
        "orphan_triggers": orphan_triggers,
        "duplicate_public_keys": duplicate_public_keys,
    }


def rollback_gate_c_test_records():
    RecognitionArtifact.query.delete()
    TriggerAsset.query.delete()
    Asset.query.delete()
    ProcessingJob.query.delete()
    Trigger.query.delete()
    Experience.query.delete()
    WorkspaceMember.query.delete()
    Workspace.query.delete()
    MigrationCheckpoint.query.filter_by(migration_name=MIGRATION_NAME).delete()
    db.session.commit()
