import pytest
from sqlalchemy.exc import IntegrityError

from public_keys import generate_unique_public_key, is_url_safe_public_key
from models import (
    Asset,
    Experience,
    Organization,
    RecognitionArtifact,
    Trigger,
    Workspace,
    WorkspaceMember,
)


def test_personal_workspace_without_organization(app_module, db_session, normal_user):
    workspace = Workspace(
        public_key=generate_unique_public_key(db_session, Workspace, "wsp"),
        name="Personal",
        workspace_type="personal",
        status="active",
    )
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=normal_user.id, role="owner"))
    db_session.commit()

    assert workspace.organization_id is None
    assert workspace.members[0].user_id == normal_user.id


def test_optional_organization_can_own_workspace(app_module, db_session):
    org = Organization(public_key=generate_unique_public_key(db_session, Organization, "org"), name="Org")
    db_session.add(org)
    db_session.flush()
    workspace = Workspace(
        public_key=generate_unique_public_key(db_session, Workspace, "wsp"),
        organization_id=org.id,
        name="Team",
        workspace_type="team",
    )
    db_session.add(workspace)
    db_session.commit()

    assert org.workspaces[0].id == workspace.id


def test_experience_and_trigger_legacy_mapping(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    workspace = Workspace(
        public_key=generate_unique_public_key(db_session, Workspace, "wsp"),
        name="Owner Workspace",
        workspace_type="personal",
    )
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=normal_user.id, role="owner"))
    experience = Experience(
        public_key=generate_unique_public_key(db_session, Experience, "exp"),
        workspace_id=workspace.id,
        legacy_project_id=project.id,
        name=project.name,
        created_by_user_id=normal_user.id,
    )
    db_session.add(experience)
    db_session.flush()
    trigger = Trigger(
        public_key=generate_unique_public_key(db_session, Trigger, "trg"),
        experience_id=experience.id,
        legacy_project_pair_id=pair.id,
        name="Pair 0",
        status="ready",
    )
    db_session.add(trigger)
    db_session.commit()

    assert project.mapped_experience.id == experience.id
    assert pair.mapped_trigger.id == trigger.id


def test_asset_and_recognition_artifact_relationship(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    workspace = Workspace(public_key=generate_unique_public_key(db_session, Workspace, "wsp"), name="Assets")
    db_session.add(workspace)
    db_session.flush()
    experience = Experience(
        public_key=generate_unique_public_key(db_session, Experience, "exp"),
        workspace_id=workspace.id,
        legacy_project_id=project.id,
        name=project.name,
    )
    db_session.add(experience)
    db_session.flush()
    trigger = Trigger(
        public_key=generate_unique_public_key(db_session, Trigger, "trg"),
        experience_id=experience.id,
        legacy_project_pair_id=pair.id,
        name="Pair",
    )
    asset = Asset(
        public_key=generate_unique_public_key(db_session, Asset, "ast"),
        workspace_id=workspace.id,
        asset_type="image",
        storage_key=pair.image_filename,
    )
    db_session.add_all([trigger, asset])
    db_session.flush()
    artifact = RecognitionArtifact(
        trigger_id=trigger.id,
        artifact_type="feature_npz",
        storage_key=f"{project.id}_{pair.pair_index}.npz",
    )
    db_session.add(artifact)
    db_session.commit()

    assert artifact.trigger_id == trigger.id
    assert asset.workspace_id == workspace.id


def test_public_keys_are_url_safe_unique_and_immutable(app_module, db_session):
    key = generate_unique_public_key(db_session, Workspace, "wsp")
    workspace = Workspace(public_key=key, name="Stable Key")
    db_session.add(workspace)
    db_session.commit()

    assert is_url_safe_public_key(key)
    assert not key.endswith(str(workspace.id))

    workspace.public_key = generate_unique_public_key(db_session, Workspace, "wsp")
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()


def test_unique_legacy_mappings_are_enforced(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    workspace = Workspace(public_key=generate_unique_public_key(db_session, Workspace, "wsp"), name="Unique")
    db_session.add(workspace)
    db_session.flush()
    first = Experience(
        public_key=generate_unique_public_key(db_session, Experience, "exp"),
        workspace_id=workspace.id,
        legacy_project_id=project.id,
        name="First",
    )
    second = Experience(
        public_key=generate_unique_public_key(db_session, Experience, "exp"),
        workspace_id=workspace.id,
        legacy_project_id=project.id,
        name="Second",
    )
    db_session.add_all([first, second])
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    exp = Experience(
        public_key=generate_unique_public_key(db_session, Experience, "exp"),
        workspace_id=workspace.id,
        legacy_project_id=project.id,
        name="First",
    )
    db_session.add(exp)
    db_session.flush()
    db_session.add_all(
        [
            Trigger(public_key=generate_unique_public_key(db_session, Trigger, "trg"), experience_id=exp.id, legacy_project_pair_id=pair.id, name="A"),
            Trigger(public_key=generate_unique_public_key(db_session, Trigger, "trg"), experience_id=exp.id, legacy_project_pair_id=pair.id, name="B"),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
