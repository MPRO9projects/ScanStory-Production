import public_keys
from compatibility_resolver import (
    resolve_experience_for_legacy_project,
    resolve_legacy_pair_for_trigger,
    resolve_legacy_project_for_experience,
    resolve_trigger_for_legacy_pair,
)
from gate_c_migration import backfill_default_workspaces, map_pairs_to_triggers, map_projects_to_experiences
from models import Experience, Trigger, Workspace


def test_legacy_resolvers_preserve_project_and_pair_identity(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)
    map_pairs_to_triggers(dry_run=False, media_exists=lambda kind, pair: True)

    legacy_project, experience = resolve_experience_for_legacy_project(project.id)
    legacy_pair, trigger = resolve_trigger_for_legacy_pair(pair.id)
    assert legacy_project.id == project.id
    assert experience.legacy_project_id == project.id
    assert legacy_pair.id == pair.id
    assert trigger.legacy_project_pair_id == pair.id

    same_experience, same_project = resolve_legacy_project_for_experience(experience.id)
    same_trigger, same_pair = resolve_legacy_pair_for_trigger(trigger.id)
    assert same_experience.id == experience.id
    assert same_project.id == project.id
    assert same_trigger.id == trigger.id
    assert same_pair.id == pair.id


def test_dual_read_does_not_change_scanner_route_response(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    before = client.get(f"/scanner/{project.id}?user_id={project.owner_user_id}&user_name=Normal")
    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)
    map_pairs_to_triggers(dry_run=False, media_exists=lambda kind, pair: True)
    after = client.get(f"/scanner/{project.id}?user_id={project.owner_user_id}&user_name=Normal")

    assert before.status_code == 200
    assert after.status_code == 200
    assert b"detect_init" in after.data


def test_user_a_project_maps_only_to_user_a_workspace(app_module, db_session, normal_user, expired_user, project_with_pair):
    project, pair = project_with_pair
    backfill_default_workspaces(dry_run=False)
    map_projects_to_experiences(dry_run=False)

    experience = Experience.query.filter_by(legacy_project_id=project.id).one()
    owner_user_ids = {member.user_id for member in experience.workspace.members}
    assert normal_user.id in owner_user_ids
    assert expired_user.id not in owner_user_ids


def test_public_key_collision_retry(app_module, db_session, monkeypatch):
    existing = Workspace(public_key="wsp_collision", name="Existing")
    db_session.add(existing)
    db_session.commit()
    values = iter(["wsp_collision", "wsp_after_collision"])
    monkeypatch.setattr(public_keys, "generate_public_key", lambda prefix: next(values))

    generated = public_keys.generate_unique_public_key(db_session, Workspace, "wsp")
    assert generated == "wsp_after_collision"


def test_existing_qr_and_pair_routes_still_pass(client, project_with_pair):
    project, pair = project_with_pair
    assert client.get(project.qr_code_path).status_code == 200
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 200
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 200
