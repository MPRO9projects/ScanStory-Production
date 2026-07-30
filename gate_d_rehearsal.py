import csv
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from gate_c_migration import rollback_gate_c_test_records, verify_gate_c_migration
from models import (
    Admin,
    Asset,
    Experience,
    MigrationCheckpoint,
    PaymentOrder,
    Project,
    ProjectPair,
    RecognitionArtifact,
    ScanLog,
    SubscriptionPlan,
    TrialDetails,
    Trigger,
    User,
    Workspace,
    WorkspaceMember,
    db,
)


OWNERSHIP_RESOLUTION_TYPES = {
    "customer_owned",
    "managed_service",
    "internal_demo",
    "test_data",
    "unknown",
}
OWNERSHIP_STATUSES = {
    "automatically_resolved",
    "manually_resolved",
    "internal_demo",
    "managed_service",
    "unresolved",
    "blocked",
}


def sanitize_database_url(database_url):
    if not database_url:
        return ""
    if "://" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    if "@" not in rest:
        return f"{scheme}://{rest}"
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def database_fingerprint(database_url):
    return hashlib.sha256(sanitize_database_url(database_url).encode("utf-8")).hexdigest()[:16]


def parse_ownership_mapping(path):
    if not path:
        return {}
    mapping_path = Path(path)
    if not mapping_path.exists():
        raise ValueError(f"ownership mapping file does not exist: {path}")
    if mapping_path.suffix.lower() == ".json":
        rows = json.loads(mapping_path.read_text(encoding="utf-8"))
    else:
        with mapping_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

    resolutions = {}
    for row in rows:
        legacy_project_id = int(row["legacy_project_id"])
        if legacy_project_id in resolutions:
            raise ValueError(f"duplicate ownership mapping for project {legacy_project_id}")
        resolution_type = row.get("resolution_type", "").strip()
        if resolution_type not in OWNERSHIP_RESOLUTION_TYPES:
            raise ValueError(f"invalid resolution_type for project {legacy_project_id}")
        target_workspace_id = row.get("target_workspace_id", "").strip()
        if resolution_type in {"customer_owned", "managed_service", "internal_demo"} and not target_workspace_id:
            raise ValueError(f"target_workspace_id required for project {legacy_project_id}")
        resolutions[legacy_project_id] = {
            "legacy_project_id": legacy_project_id,
            "resolution_type": resolution_type,
            "target_workspace_id": int(target_workspace_id) if target_workspace_id else None,
            "customer_reference": row.get("customer_reference", ""),
            "resolved_by": row.get("resolved_by", ""),
            "resolution_note": row.get("resolution_note", ""),
        }
    return resolutions


def profile_source_data(media_exists=None):
    projects = Project.query.all()
    pairs = ProjectPair.query.all()
    missing_image = 0
    missing_video = 0
    missing_feature = 0
    if media_exists:
        for pair in pairs:
            missing_image += int(not media_exists("image", pair))
            missing_video += int(not media_exists("video", pair))
            missing_feature += int(not media_exists("feature_npz", pair))

    return {
        "users": User.query.count(),
        "admins": Admin.query.count(),
        "projects": len(projects),
        "project_pairs": len(pairs),
        "scan_logs": ScanLog.query.count(),
        "payments": PaymentOrder.query.count(),
        "plans": SubscriptionPlan.query.count(),
        "trials": TrialDetails.query.count(),
        "user_owned_projects": sum(1 for p in projects if p.owner_user_id and not p.owner_admin_id),
        "admin_owned_projects": sum(1 for p in projects if p.owner_admin_id and not p.owner_user_id),
        "missing_owner_projects": sum(1 for p in projects if not p.owner_user_id and not p.owner_admin_id),
        "projects_with_zero_pairs": sum(1 for p in projects if len(p.pairs) == 0),
        "projects_with_multiple_pairs": sum(1 for p in projects if len(p.pairs) > 1),
        "pairs_missing_image_path": sum(1 for p in pairs if not p.image_filename or not p.image_path),
        "pairs_missing_video_path": sum(1 for p in pairs if not p.video_filename),
        "pairs_missing_image_file": missing_image,
        "pairs_missing_video_file": missing_video,
        "pairs_missing_feature_file": missing_feature,
        "duplicate_emails": db.session.query(User.email).group_by(User.email).having(db.func.count(User.id) > 1).count(),
        "malformed_emails": User.query.filter(~User.email.contains("@")).count(),
    }


def classify_source_profile(profile):
    rows = []
    for key, value in profile.items():
        severity = "clean"
        if key in {"admin_owned_projects", "missing_owner_projects"} and value:
            severity = "requires manual resolution"
        elif key in {"pairs_missing_image_file", "pairs_missing_video_file", "pairs_missing_feature_file"} and value:
            severity = "warning"
        elif key in {"duplicate_emails", "malformed_emails"} and value:
            severity = "blocks migration"
        elif value:
            severity = "clean"
        rows.append({"metric": key, "value": value, "severity": severity})
    return rows


def reconcile_after_migration(eligible_projects=None, eligible_pairs=None):
    verification = verify_gate_c_migration()
    eligible_projects = Project.query.count() if eligible_projects is None else eligible_projects
    eligible_pairs = ProjectPair.query.count() if eligible_pairs is None else eligible_pairs
    verification["eligible_projects"] = eligible_projects
    verification["eligible_pairs"] = eligible_pairs
    verification["project_mapping_mismatch"] = max(0, eligible_projects - verification["mapped_experiences"])
    verification["pair_mapping_mismatch"] = max(0, eligible_pairs - verification["mapped_triggers"])
    verification["asset_count"] = Asset.query.count()
    verification["recognition_artifact_count"] = RecognitionArtifact.query.count()
    return verification


def rollback_rehearsal(dry_run=True, allow_rehearsal=False):
    if not allow_rehearsal:
        raise RuntimeError("rollback rehearsal requires allow_rehearsal=True")
    counts = {
        "recognition_artifacts": RecognitionArtifact.query.count(),
        "assets": Asset.query.count(),
        "triggers": Trigger.query.count(),
        "experiences": Experience.query.count(),
        "workspace_members": WorkspaceMember.query.count(),
        "workspaces": Workspace.query.count(),
        "checkpoints": MigrationCheckpoint.query.count(),
    }
    if not dry_run:
        rollback_gate_c_test_records()
    return counts


def sanitized_run_log(command, database_url, result, started_at=None, ended_at=None, exit_status=0):
    started_at = started_at or time.time()
    ended_at = ended_at or time.time()
    errors = getattr(result, "errors", []) if result is not None else []
    return {
        "run_id": str(uuid.uuid4()),
        "start_time": started_at,
        "end_time": ended_at,
        "environment": "rehearsal",
        "database": sanitize_database_url(database_url),
        "database_fingerprint": database_fingerprint(database_url),
        "command": command,
        "created": getattr(result, "created", {}),
        "existing": getattr(result, "existing", {}),
        "skipped": getattr(result, "skipped", {}),
        "failed_count": len(errors),
        "warnings": [err.get("error", "") for err in errors],
        "exit_status": exit_status,
    }


def database_file_size(database_url):
    if not database_url.startswith("sqlite:///"):
        return None
    path = database_url.replace("sqlite:///", "", 1)
    return os.path.getsize(path) if os.path.exists(path) else 0
