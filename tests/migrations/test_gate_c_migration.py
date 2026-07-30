import pytest
from sqlalchemy.exc import IntegrityError

from gate_c_migration import (
    backfill_default_workspaces,
    map_pairs_to_triggers,
    map_projects_to_experiences,
    rollback_gate_c_test_records,
    run_gate_c_migration,
    verify_gate_c_migration,
)
from models import Experience, MigrationCheckpoint, Project, ProjectPair, Trigger, Workspace, WorkspaceMember


def test_workspace_backfill_one_user_rerun_and_checkpoint(app_module, db_session, normal_user):
    dry = backfill_default_workspaces(dry_run=True)
    assert dry.created["workspaces"] == 1
    assert Workspace.query.count() == 0

    applied = backfill_default_workspaces(dry_run=False)
    assert applied.created["workspaces"] == 1
    assert WorkspaceMember.query.filter_by(user_id=normal_user.id, role="owner").count() == 1
    assert MigrationCheckpoint.query.filter_by(entity_type="user", legacy_id=normal_user.id, status="completed").count() == 1

    rerun = backfill_default_workspaces(dry_run=False)
    assert rerun.existing["workspaces"] == 1
    assert WorkspaceMember.query.filter_by(user_id=normal_user.id, role="owner").count() == 1


def test_workspace_backfill_multiple_users(app_module, db_session, normal_user, expired_user):
    result = backfill_default_workspaces(dry_run=False)
    assert result.created["workspaces"] == 2
    assert Workspace.query.count() == 2


def test_project_mapping_uses_owner_workspace_and_is_idempotent(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    backfill_default_workspaces(dry_run=False)

    dry = map_projects_to_experiences(dry_run=True)
    assert dry.created["experiences"] == 1
    assert Experience.query.count() == 0

    applied = map_projects_to_experiences(dry_run=False)
    assert applied.created["experiences"] == 1
    experience = Experience.query.filter_by(legacy_project_id=project.id).one()
    assert experience.workspace_id == WorkspaceMember.query.filter_by(user_id=normal_user.id, role="owner").one().workspace_id

    rerun = map_projects_to_experiences(dry_run=False)
    assert rerun.existing["experiences"] == 1
    assert Experience.query.filter_by(legacy_project_id=project.id).count() == 1


def test_project_mapping_reports_missing_owner_and_admin_owned(app_module, db_session, admin):
    no_owner = Project(name="No Owner")
    admin_project = Project(name="Admin Owned", owner_admin_id=admin.id)
    db_session.add_all([no_owner, admin_project])
    db_session.commit()

    result = map_projects_to_experiences(dry_run=False)
    messages = {item["legacy_id"]: item["error"] for item in result.errors}
    assert "no user owner" in messages[no_owner.id]
    assert "admin-owned" in messages[admin_project.id]
    assert MigrationCheckpoint.query.filter_by(entity_type="project", status="failed").count() == 2


def test_duplicate_project_mapping_is_rejected(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    workspace = Workspace(public_key="wsp_unique_project", name="Workspace")
    db_session.add(workspace)
    db_session.flush()
    db_session.add_all(
        [
            Experience(public_key="exp_dup_one", workspace_id=workspace.id, legacy_project_id=project.id, name="One"),
            Experience(public_key="exp_dup_two", workspace_id=workspace.id, legacy_project_id=project.id, name="Two"),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_pair_mapping_creates_trigger_assets_artifact_and_is_idempotent(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)

    result = map_pairs_to_triggers(dry_run=False, media_exists=lambda kind, pair: True)
    assert result.created["triggers"] == 1
    trigger = Trigger.query.filter_by(legacy_project_pair_id=pair.id).one()
    assert trigger.experience.legacy_project_id == project.id
    assert len(trigger.trigger_assets) == 2
    assert trigger.recognition_artifacts[0].storage_key == f"{project.id}_{pair.pair_index}.npz"

    rerun = map_pairs_to_triggers(dry_run=False, media_exists=lambda kind, pair: True)
    assert rerun.existing["triggers"] == 1
    assert Trigger.query.filter_by(legacy_project_pair_id=pair.id).count() == 1


def test_pair_mapping_reports_missing_project_mapping_and_missing_files(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    missing_mapping = map_pairs_to_triggers(dry_run=False)
    assert missing_mapping.errors[0]["error"] == "missing mapped experience"

    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)
    with_missing_files = map_pairs_to_triggers(dry_run=True, media_exists=lambda kind, pair: False)
    assert "missing image file" in with_missing_files.errors[0]["error"]
    assert "missing feature artifact" in with_missing_files.errors[0]["error"]


def test_duplicate_pair_mapping_is_rejected(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)
    experience = Experience.query.filter_by(legacy_project_id=project.id).one()
    db_session.add_all(
        [
            Trigger(public_key="trg_dup_one", experience_id=experience.id, legacy_project_pair_id=pair.id, name="One"),
            Trigger(public_key="trg_dup_two", experience_id=experience.id, legacy_project_pair_id=pair.id, name="Two"),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_verification_and_test_rollback(app_module, db_session, project_with_pair):
    run_gate_c_migration(dry_run=False, media_exists=lambda kind, pair: True)
    verification = verify_gate_c_migration()
    assert verification["users"] == 1
    assert verification["mapped_experiences"] == 1
    assert verification["mapped_triggers"] == 1
    assert verification["duplicate_public_keys"] == 0

    rollback_gate_c_test_records()
    assert Workspace.query.count() == 0
    assert Experience.query.count() == 0
    assert Trigger.query.count() == 0
    assert ProjectPair.query.count() == 1
