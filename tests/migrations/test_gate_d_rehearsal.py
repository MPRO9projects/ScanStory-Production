import json

import pytest

from gate_c_migration import backfill_default_workspaces, map_pairs_to_triggers, map_projects_to_experiences, run_gate_c_migration
from gate_d_rehearsal import (
    parse_ownership_mapping,
    profile_source_data,
    reconcile_after_migration,
    rollback_rehearsal,
    sanitized_run_log,
)
from migration_gate_c import _configure_database_url, main
from models import Experience, Project, ProjectPair, Trigger, Workspace


def _write_mapping(tmp_path, rows):
    path = tmp_path / "ownership.csv"
    path.write_text(
        "legacy_project_id,resolution_type,target_workspace_id,customer_reference,resolved_by,resolution_note\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    return path


def test_ownership_mapping_file_parsing(tmp_path):
    path = _write_mapping(tmp_path, ["42,managed_service,7,CUST-1,ops@example.com,approved"])
    parsed = parse_ownership_mapping(path)
    assert parsed[42]["resolution_type"] == "managed_service"
    assert parsed[42]["target_workspace_id"] == 7


def test_invalid_duplicate_and_missing_ownership_mapping(tmp_path):
    invalid = _write_mapping(tmp_path, ["1,bad_type,7,CUST,ops,note"])
    with pytest.raises(ValueError):
        parse_ownership_mapping(invalid)

    duplicate = _write_mapping(tmp_path, ["1,internal_demo,7,,ops,note", "1,managed_service,8,CUST,ops,note"])
    with pytest.raises(ValueError):
        parse_ownership_mapping(duplicate)

    missing_target = _write_mapping(tmp_path, ["2,managed_service,,CUST,ops,note"])
    with pytest.raises(ValueError):
        parse_ownership_mapping(missing_target)


def test_json_ownership_mapping_file_parsing(tmp_path):
    path = tmp_path / "ownership.json"
    path.write_text(
        json.dumps(
            [
                {
                    "legacy_project_id": "3",
                    "resolution_type": "internal_demo",
                    "target_workspace_id": "9",
                    "customer_reference": "",
                    "resolved_by": "ops@example.com",
                    "resolution_note": "demo",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert parse_ownership_mapping(path)[3]["resolution_type"] == "internal_demo"


def test_admin_owned_project_resolves_only_with_explicit_workspace(app_module, db_session, admin):
    workspace = Workspace(public_key="wsp_internal_demo", name="Internal Demo", workspace_type="managed_service")
    db_session.add(workspace)
    db_session.flush()
    project = Project(name="Admin Project", owner_admin_id=admin.id)
    db_session.add(project)
    db_session.commit()

    unresolved = map_projects_to_experiences(dry_run=False)
    assert "requires explicit workspace" in unresolved.errors[0]["error"]

    resolved = map_projects_to_experiences(
        dry_run=False,
        ownership_resolutions={project.id: {"resolution_type": "internal_demo", "target_workspace_id": workspace.id}},
    )
    assert resolved.created["experiences"] == 1
    assert Experience.query.filter_by(legacy_project_id=project.id, workspace_id=workspace.id).count() == 1


def test_unknown_workspace_target_is_reported(app_module, db_session, admin):
    project = Project(name="Admin Project", owner_admin_id=admin.id)
    db_session.add(project)
    db_session.commit()

    result = map_projects_to_experiences(
        dry_run=False,
        ownership_resolutions={project.id: {"resolution_type": "managed_service", "target_workspace_id": 99999}},
    )
    assert "unknown workspace" in result.errors[0]["error"]
    assert Experience.query.count() == 0


def test_unknown_ownership_remains_unresolved(app_module, db_session):
    project = Project(name="Unknown")
    db_session.add(project)
    db_session.commit()
    result = map_projects_to_experiences(dry_run=False)
    assert "no user owner" in result.errors[0]["error"]
    assert Experience.query.count() == 0


def test_orphan_and_missing_media_are_visible(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    profile = profile_source_data(media_exists=lambda kind, pair: False)
    assert profile["pairs_missing_image_file"] == 1
    assert profile["pairs_missing_video_file"] == 1
    assert profile["pairs_missing_feature_file"] == 1

    missing_mapping = map_pairs_to_triggers(dry_run=False)
    assert missing_mapping.errors[0]["error"] == "missing mapped experience"


def test_partial_failure_and_rerun_after_failure(app_module, db_session, normal_user, admin, project_with_pair):
    user_project, pair = project_with_pair
    admin_project = Project(name="Admin Project", owner_admin_id=admin.id)
    db_session.add(admin_project)
    db_session.commit()

    first = run_gate_c_migration(dry_run=False, media_exists=lambda kind, pair: True)
    assert first.created["experiences"] == 1
    assert first.skipped["projects"] == 1

    workspace = Workspace(public_key="wsp_customer_fix", name="Customer Fix", workspace_type="managed_service")
    db_session.add(workspace)
    db_session.commit()
    second = map_projects_to_experiences(
        dry_run=False,
        ownership_resolutions={admin_project.id: {"resolution_type": "managed_service", "target_workspace_id": workspace.id}},
    )
    assert second.created["experiences"] == 1
    assert Experience.query.count() == 2


def test_reconciliation_mismatch_and_duplicate_counts(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    reconciliation = reconcile_after_migration(eligible_projects=1, eligible_pairs=1)
    assert reconciliation["project_mapping_mismatch"] == 1
    assert reconciliation["pair_mapping_mismatch"] == 1


def test_rollback_rehearsal_requires_flag_and_preserves_legacy(app_module, db_session, project_with_pair):
    with pytest.raises(RuntimeError):
        rollback_rehearsal(dry_run=True, allow_rehearsal=False)

    run_gate_c_migration(dry_run=False, media_exists=lambda kind, pair: True)
    before = rollback_rehearsal(dry_run=True, allow_rehearsal=True)
    assert before["workspaces"] == 1
    rollback_rehearsal(dry_run=False, allow_rehearsal=True)
    assert Workspace.query.count() == 0
    assert Project.query.count() == 1
    assert ProjectPair.query.count() == 1


def test_log_sanitization_run_id_and_no_secrets():
    result = type("Result", (), {"created": {"workspaces": 1}, "existing": {}, "skipped": {}, "errors": []})()
    log = sanitized_run_log("apply", "postgresql://user:secret@example.com/db", result, exit_status=0)
    assert log["run_id"]
    assert "secret" not in log["database"]
    assert log["database_fingerprint"]


def test_database_url_safety_guard(monkeypatch):
    monkeypatch.delenv("SCANSTORY_ALLOW_GATE_C_PRODUCTION", raising=False)
    with pytest.raises(RuntimeError):
        _configure_database_url("postgresql://user:secret@example.com/prod")


def test_cli_refuses_missing_database_url():
    assert main(["status"]) == 1
