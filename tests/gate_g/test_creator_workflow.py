from io import BytesIO
from time import perf_counter

import pytest

from models import Experience, ProcessingEvent, ProcessingJob, Trigger, Workspace, WorkspaceMember, db


ALL_FLAGS = {
    "ENABLE_EXPERIENCE_CREATOR": "1",
    "ENABLE_TRIGGER_MANAGEMENT": "1",
    "ENABLE_PROCESSING_STATUS_UI": "1",
    "ENABLE_EXPERIENCE_QR_ASSET": "1",
}


@pytest.fixture()
def gate_g_enabled(monkeypatch, tmp_path):
    for key, value in ALL_FLAGS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SCANSTORY_EXPERIENCE_STORAGE_ROOT", str(tmp_path / "experience-storage"))


def login(client, user):
    with client.session_transaction() as session:
        session["user_id"] = user.id
        session["user_email"] = user.email


def upload_pair(image_name="marker.jpg", video_name="clip.mp4"):
    return {
        "reference_images": (BytesIO(b"fake image bytes"), image_name),
        "videos": (BytesIO(b"fake video bytes"), video_name),
    }


def create_experience(client, user, name="Museum Walk"):
    login(client, user)
    response = client.post("/experiences/new", data={"name": name, "description": "Draft"}, follow_redirects=False)
    assert response.status_code == 302
    return Experience.query.filter_by(name=name).one()


def test_feature_flags_default_disabled(client, normal_user):
    login(client, normal_user)
    assert client.get("/experiences").status_code == 404
    assert client.get("/projects").status_code == 200


def test_experience_create_list_search_sort_and_accessibility(client, normal_user, gate_g_enabled):
    login(client, normal_user)
    assert client.get("/experiences").status_code == 200
    invalid = client.post("/experiences/new", data={"name": ""})
    assert invalid.status_code == 400
    experience = create_experience(client, normal_user, "Gallery Alpha")
    assert experience.public_key.startswith("exp_")
    assert experience.current_published_version_id is None
    assert WorkspaceMember.query.filter_by(user_id=normal_user.id, workspace_id=experience.workspace_id).one().role == "owner"

    html = client.get("/experiences?q=Gallery&sort=name&per_page=10").get_data(as_text=True)
    assert "Gallery Alpha" in html
    assert 'label for="q"' in html
    assert "Create Experience" in html
    assert "QR prepared foundation" in html


def test_single_and_multi_trigger_creation_queue_jobs(client, normal_user, gate_g_enabled):
    experience = create_experience(client, normal_user)
    response = client.post(f"/experiences/{experience.id}/triggers/new", data=upload_pair(), content_type="multipart/form-data")
    assert response.status_code == 302
    trigger = Trigger.query.filter_by(experience_id=experience.id).one()
    job_types = {job.job_type for job in ProcessingJob.query.filter_by(trigger_id=trigger.id).all()}
    assert {"validate_reference_image", "probe_video", "extract_recognition_artifact", "test_marker_robustness", "verify_processing_readiness"} <= job_types

    bad = client.post(
        f"/experiences/{experience.id}/triggers/new",
        data={"reference_images": [(BytesIO(b"one"), "one.jpg"), (BytesIO(b"two"), "two.jpg")], "videos": (BytesIO(b"one"), "one.mp4")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400

    response = client.post(
        f"/experiences/{experience.id}/triggers/new",
        data={"reference_images": [(BytesIO(b"a"), "a.jpg"), (BytesIO(b"b"), "b.jpg")], "videos": [(BytesIO(b"a"), "a.mp4"), (BytesIO(b"b"), "b.mp4")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert Trigger.query.filter_by(experience_id=experience.id).count() == 3


def test_status_actions_and_history_are_creator_safe(client, normal_user, gate_g_enabled):
    experience = create_experience(client, normal_user)
    client.post(f"/experiences/{experience.id}/triggers/new", data=upload_pair(), content_type="multipart/form-data")
    trigger = Trigger.query.filter_by(experience_id=experience.id).one()

    payload = client.get(f"/experiences/{experience.id}/processing-status").get_json()
    assert payload["experience"]["trigger_count"] == 1
    assert "diagnostics" not in payload["triggers"][0]

    assert client.post(f"/experiences/{experience.id}/triggers/{trigger.id}/retry").status_code == 302
    assert client.post(
        f"/experiences/{experience.id}/triggers/{trigger.id}/replace-image",
        data={"replacement_image": (BytesIO(b"new image"), "new.jpg")},
        content_type="multipart/form-data",
    ).status_code == 302
    image_jobs = {job.job_type for job in ProcessingJob.query.filter_by(trigger_id=trigger.id).all()}
    assert "extract_recognition_artifact" in image_jobs

    before = ProcessingJob.query.filter_by(trigger_id=trigger.id, job_type="regenerate_recognition_artifact").count()
    assert client.post(
        f"/experiences/{experience.id}/triggers/{trigger.id}/replace-video",
        data={"replacement_video": (BytesIO(b"new video"), "new.mp4")},
        content_type="multipart/form-data",
    ).status_code == 302
    after = ProcessingJob.query.filter_by(trigger_id=trigger.id, job_type="regenerate_recognition_artifact").count()
    assert after == before

    assert client.post(f"/experiences/{experience.id}/triggers/{trigger.id}/regenerate-recognition").status_code == 302
    assert ProcessingJob.query.filter_by(trigger_id=trigger.id, job_type="regenerate_recognition_artifact").count() == 1
    assert client.post(f"/experiences/{experience.id}/regenerate-qr").status_code == 302
    qr_events = ProcessingEvent.query.filter_by(experience_id=experience.id, event_type="qr_asset_regenerated").all()
    assert qr_events and "processing-ready-disabled" not in (qr_events[0].diagnostic_json or "")

    assert client.post(f"/experiences/{experience.id}/triggers/{trigger.id}/exclude").status_code == 302
    db.session.refresh(trigger)
    assert trigger.is_excluded is True and trigger.status == "excluded"
    assert client.post(f"/experiences/{experience.id}/triggers/{trigger.id}/reactivate").status_code == 302
    db.session.refresh(trigger)
    assert trigger.is_excluded is False
    html = client.get(f"/experiences/{experience.id}").get_data(as_text=True)
    assert "Processing History" in html
    assert "internal_diagnostics" not in html


def test_authorization_cross_workspace_and_unauthenticated(client, normal_user, expired_user, gate_g_enabled):
    experience = create_experience(client, normal_user)
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/experiences").status_code == 302
    login(client, expired_user)
    assert client.get(f"/experiences/{experience.id}").status_code == 403


def test_reviewer_read_only(client, normal_user, gate_g_enabled):
    experience = create_experience(client, normal_user)
    reviewer = WorkspaceMember.query.filter_by(user_id=normal_user.id, workspace_id=experience.workspace_id).one()
    reviewer.role = "reviewer"
    db.session.commit()
    assert client.get(f"/experiences/{experience.id}").status_code == 200
    assert client.get(f"/experiences/{experience.id}/triggers/new").status_code == 403


@pytest.mark.parametrize("count", [30, 100, 500])
def test_experience_list_is_paginated_for_synthetic_counts(client, normal_user, gate_g_enabled, count):
    login(client, normal_user)
    workspace = Workspace(public_key=f"wsp_perf_{count}", name=f"Perf {count}", workspace_type="personal", status="active")
    db.session.add(workspace)
    db.session.flush()
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=normal_user.id, role="owner", status="active"))
    for index in range(count):
        db.session.add(Experience(public_key=f"exp_perf_{count}_{index}", workspace_id=workspace.id, name=f"Experience {index}", status="draft", created_by_user_id=normal_user.id))
    db.session.commit()
    start = perf_counter()
    response = client.get("/experiences?per_page=20")
    elapsed = perf_counter() - start
    assert response.status_code == 200
    assert elapsed < 2.5
    assert response.get_data(as_text=True).count("<article") <= 20


@pytest.mark.parametrize("count", [30, 100])
def test_detail_and_status_are_bounded_for_trigger_counts(client, normal_user, gate_g_enabled, count):
    experience = create_experience(client, normal_user, f"Trigger Perf {count}")
    for index in range(count):
        db.session.add(Trigger(public_key=f"trg_perf_{count}_{index}", experience_id=experience.id, name=f"Trigger {index}", status="ready"))
    db.session.commit()
    start = perf_counter()
    detail = client.get(f"/experiences/{experience.id}")
    status = client.get(f"/experiences/{experience.id}/processing-status")
    elapsed = perf_counter() - start
    assert detail.status_code == 200
    assert status.status_code == 200
    assert elapsed < 3.0
    assert len(status.get_json()["triggers"]) == count
