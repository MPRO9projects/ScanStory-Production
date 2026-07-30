import pytest

from models import (
    Asset,
    Experience,
    ExperienceVersion,
    ExperienceVersionTrigger,
    ProcessingJob,
    RecognitionArtifact,
    Trigger,
    TriggerAsset,
    Workspace,
    WorkspaceMember,
    db,
)
from public_keys import generate_unique_public_key


FLAGS = {
    "ENABLE_EXPERIENCE_CREATOR": "1",
    "ENABLE_EXPERIENCE_PUBLISHING": "1",
    "ENABLE_PUBLIC_EXPERIENCE_ROUTE": "1",
    "ENABLE_VERSION_ROLLBACK": "1",
    "ENABLE_EXPERIENCE_PAUSE": "1",
}


REQUIRED_JOBS = [
    "validate_reference_image",
    "probe_video",
    "extract_recognition_artifact",
    "test_marker_robustness",
    "verify_processing_readiness",
]


@pytest.fixture()
def gate_h_enabled(monkeypatch):
    for key, value in FLAGS.items():
        monkeypatch.setenv(key, value)


def login(client, user):
    with client.session_transaction() as session:
        session["user_id"] = user.id
        session["user_email"] = user.email


def make_workspace(user, role="owner"):
    workspace = Workspace(public_key=generate_unique_public_key(db.session, Workspace, "wsp"), name="Publish Workspace", workspace_type="personal", status="active")
    db.session.add(workspace)
    db.session.flush()
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role, status="active"))
    db.session.commit()
    return workspace


def make_experience(user, role="owner", name="Publishable"):
    workspace = make_workspace(user, role)
    experience = Experience(
        public_key=generate_unique_public_key(db.session, Experience, "exp"),
        workspace_id=workspace.id,
        name=name,
        status="draft",
        created_by_user_id=user.id,
    )
    db.session.add(experience)
    db.session.commit()
    return experience


def add_asset(workspace, kind, filename):
    asset = Asset(
        public_key=generate_unique_public_key(db.session, Asset, "ast"),
        workspace_id=workspace.id,
        asset_type=kind,
        storage_provider="local_legacy",
        storage_key=f"workspaces/{workspace.public_key}/{filename}",
        original_filename=filename,
        mime_type="video/mp4" if kind == "video" else "image/jpeg",
        size_bytes=10,
    )
    db.session.add(asset)
    db.session.flush()
    return asset


def add_ready_trigger(experience, label="Marker", video_name="video-a.mp4"):
    image = add_asset(experience.workspace, "image", f"{label}-image.jpg")
    video = add_asset(experience.workspace, "video", video_name)
    trigger = Trigger(public_key=generate_unique_public_key(db.session, Trigger, "trg"), experience_id=experience.id, name=label, status="ready")
    db.session.add(trigger)
    db.session.flush()
    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=image.id, role="reference_image"))
    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=video.id, role="video"))
    artifact = RecognitionArtifact(trigger_id=trigger.id, artifact_type="feature_npz", storage_key=f"recognition/{trigger.public_key}.npz", status="available")
    db.session.add(artifact)
    db.session.flush()
    for job_type in REQUIRED_JOBS:
        db.session.add(
            ProcessingJob(
                public_key=generate_unique_public_key(db.session, ProcessingJob, "job"),
                workspace_id=experience.workspace_id,
                experience_id=experience.id,
                trigger_id=trigger.id,
                job_type=job_type,
                status="succeeded",
                progress=100,
                idempotency_key=f"{trigger.public_key}:{job_type}",
            )
        )
    db.session.commit()
    return trigger, image, video, artifact


def publish_ready_experience(experience, user, key="publish-1"):
    from publishing import ensure_draft_version, evaluate_publish_readiness, publish_experience_version

    draft = ensure_draft_version(experience.id, user.id)
    readiness = evaluate_publish_readiness(experience.id, draft.id, user.id)
    assert readiness["ready"], readiness
    return publish_experience_version(experience.id, draft.id, user.id, key, readiness["checksum"])["version"]


def test_publishing_flags_default_disabled(client, normal_user):
    experience = make_experience(normal_user)
    add_ready_trigger(experience)
    from publishing import PublishingError, ensure_draft_version, publish_experience_version

    draft = ensure_draft_version(experience.id, normal_user.id)
    with pytest.raises(PublishingError):
        publish_experience_version(experience.id, draft.id, normal_user.id, "disabled")
    assert client.get(f"/e/{experience.public_key}").status_code == 404


def test_first_publish_snapshot_immutable_and_public_route(client, normal_user, gate_h_enabled):
    experience = make_experience(normal_user)
    trigger, image, video, artifact = add_ready_trigger(experience, video_name="video-a.mp4")
    version = publish_ready_experience(experience, normal_user)
    db.session.refresh(experience)
    assert version.version_number == 1
    assert version.status == "published"
    assert version.is_immutable is True
    assert experience.current_published_version_id == version.id
    snapshot = ExperienceVersionTrigger.query.filter_by(experience_version_id=version.id).one()
    assert snapshot.video_asset_id == video.id
    snapshot.creator_label = "Mutate"
    with pytest.raises(ValueError):
        db.session.commit()
    db.session.rollback()

    html = client.get(f"/e/{experience.public_key}").get_data(as_text=True)
    assert "Published Version 1" in html
    assert "video-a.mp4" in html
    assert "Draft" not in html


def test_same_qr_video_update_and_rollback(client, normal_user, gate_h_enabled):
    experience = make_experience(normal_user)
    trigger, image, video_a, artifact = add_ready_trigger(experience, video_name="video-a.mp4")
    version1 = publish_ready_experience(experience, normal_user, "publish-v1")
    destination = f"/e/{experience.public_key}"
    assert "video-a.mp4" in client.get(destination).get_data(as_text=True)

    video_b = add_asset(experience.workspace, "video", "video-b.mp4")
    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=video_b.id, role="video"))
    db.session.commit()
    assert "video-a.mp4" in client.get(destination).get_data(as_text=True)
    assert "video-b.mp4" not in client.get(destination).get_data(as_text=True)

    version2 = publish_ready_experience(experience, normal_user, "publish-v2")
    assert version2.version_number == 2
    html = client.get(destination).get_data(as_text=True)
    assert "Published Version 2" in html
    assert "video-b.mp4" in html
    assert f"/e/{experience.public_key}" == destination
    assert ExperienceVersionTrigger.query.filter_by(experience_version_id=version1.id).one().video_asset_id == video_a.id
    assert ExperienceVersionTrigger.query.filter_by(experience_version_id=version2.id).one().video_asset_id == video_b.id

    from publishing import rollback_experience_to_version

    rollback_experience_to_version(experience.id, version1.id, normal_user.id, "rollback-v1")
    assert "Published Version 1" in client.get(destination).get_data(as_text=True)
    assert "video-a.mp4" in client.get(destination).get_data(as_text=True)


def test_publish_readiness_blocks_failed_active_but_allows_excluded(client, normal_user, gate_h_enabled):
    experience = make_experience(normal_user)
    trigger, image, video, artifact = add_ready_trigger(experience)
    trigger.status = "failed"
    db.session.commit()
    from publishing import ensure_draft_version, evaluate_publish_readiness

    draft = ensure_draft_version(experience.id, normal_user.id)
    blocked = evaluate_publish_readiness(experience.id, draft.id, normal_user.id)
    assert not blocked["ready"]
    assert any("failed" in issue for issue in blocked["issues"])
    trigger.is_excluded = True
    db.session.commit()
    allowed = evaluate_publish_readiness(experience.id, draft.id, normal_user.id)
    assert not allowed["ready"]
    assert "no_active_included_triggers" in allowed["issues"]


def test_authorization_publisher_can_publish_creator_cannot(client, normal_user, expired_user, gate_h_enabled):
    experience = make_experience(normal_user, role="publisher")
    add_ready_trigger(experience)
    assert publish_ready_experience(experience, normal_user, "publisher-key").status == "published"

    creator_exp = make_experience(expired_user, role="creator")
    add_ready_trigger(creator_exp)
    from publishing import AuthorizationError, ensure_draft_version, publish_experience_version

    draft = ensure_draft_version(creator_exp.id, expired_user.id)
    with pytest.raises(AuthorizationError):
        publish_experience_version(creator_exp.id, draft.id, expired_user.id, "creator-key")


def test_idempotency_and_cross_workspace_denial(client, normal_user, expired_user, gate_h_enabled):
    experience = make_experience(normal_user)
    add_ready_trigger(experience)
    from publishing import AuthorizationError, ensure_draft_version, evaluate_publish_readiness, publish_experience_version

    draft = ensure_draft_version(experience.id, normal_user.id)
    readiness = evaluate_publish_readiness(experience.id, draft.id, normal_user.id)
    first = publish_experience_version(experience.id, draft.id, normal_user.id, "same-key", readiness["checksum"])
    repeat = publish_experience_version(experience.id, draft.id, normal_user.id, "same-key", readiness["checksum"])
    assert first["version"].id == repeat["version"].id
    assert repeat["idempotent"] is True
    with pytest.raises(AuthorizationError):
        publish_experience_version(experience.id, draft.id, expired_user.id, "other-user")


def test_pause_resume_archive_public_fallback(client, normal_user, gate_h_enabled):
    experience = make_experience(normal_user)
    add_ready_trigger(experience)
    publish_ready_experience(experience, normal_user)
    from publishing import set_experience_public_state

    set_experience_public_state(experience.id, "paused", normal_user.id)
    paused = client.get(f"/e/{experience.public_key}")
    assert paused.status_code == 503
    assert "paused" in paused.get_data(as_text=True)
    set_experience_public_state(experience.id, "ready_to_publish", normal_user.id)
    assert client.get(f"/e/{experience.public_key}").status_code == 200
    set_experience_public_state(experience.id, "archived", normal_user.id)
    archived = client.get(f"/e/{experience.public_key}")
    assert archived.status_code == 410
    assert "archived" in archived.get_data(as_text=True)


@pytest.mark.parametrize("count", [1, 30, 100])
def test_publish_and_public_resolver_bounded(client, normal_user, gate_h_enabled, count):
    experience = make_experience(normal_user, name=f"Perf {count}")
    for index in range(count):
        add_ready_trigger(experience, label=f"Marker {index}", video_name=f"video-{index}.mp4")
    version = publish_ready_experience(experience, normal_user, f"publish-perf-{count}")
    assert ExperienceVersionTrigger.query.filter_by(experience_version_id=version.id).count() == count
    response = client.get(f"/e/{experience.public_key}")
    assert response.status_code == 200
    assert response.get_data(as_text=True).count("<article") == count
