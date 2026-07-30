import csv
import hashlib
from pathlib import Path


OWNER_RESOLUTION_TYPES = {
    "customer_workspace",
    "managed_service_workspace",
    "internal_demo_workspace",
    "unresolved",
    "exclude_from_migration",
}
OWNER_APPROVAL_STATUSES = {"draft", "approved", "rejected"}


def checksum_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_owner_resolution_file(path):
    required = {
        "legacy_project_id",
        "resolution_type",
        "customer_reference",
        "ownership_status",
        "resolved_by",
        "resolved_at",
        "resolution_note",
        "approval_status",
        "approved_by",
    }
    rows = []
    seen = set()
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            legacy_project_id = int(row["legacy_project_id"])
            if legacy_project_id in seen:
                raise ValueError(f"duplicate legacy_project_id: {legacy_project_id}")
            seen.add(legacy_project_id)
            if row["resolution_type"] not in OWNER_RESOLUTION_TYPES:
                raise ValueError(f"invalid resolution_type: {row['resolution_type']}")
            if row["approval_status"] not in OWNER_APPROVAL_STATUSES:
                raise ValueError(f"invalid approval_status: {row['approval_status']}")
            if row["resolution_type"] not in {"unresolved", "exclude_from_migration"}:
                if not row.get("target_workspace_public_key") and not row.get("target_workspace_id"):
                    raise ValueError("target workspace required for resolved ownership")
            rows.append(row)
    return {"rows": len(rows), "checksum": checksum_file(path)}
