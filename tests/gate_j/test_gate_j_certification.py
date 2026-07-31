from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from models import (
    Asset,
    Experience,
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
from scanner_runtime import RecognitionRequestPolicy, ScannerStateMachine, mode_config, select_runtime_mode


FLAGS = {
    "ENABLE_EXPERIENCE_CREATOR": "1",
    "ENABLE_EXPERIENCE_PUBLISHING": "1",
    "ENABLE_PUBLIC_EXPERIENCE_ROUTE": "1",
    "ENABLE_VERSION_ROLLBACK": "1",
}

REQUIRED_JOBS = [
    "validate_reference_image",
    "probe_video",
    "extract_recognition_artifact",
    "test_marker_robustness",
    "verify_processing_readiness",
]


@pytest.fixture()
def gate_j_enabled(monkeypatch):
    for key, value in FLAGS.items():
        monkeypatch.setenv(key, value)


def make_workspace(user, role="owner"):
    workspace = Workspace(public_key=generate_unique_public_key(db.session, Workspace, "wsp"), name="Gate J Workspace", workspace_type="personal", status="active")
    db.session.add(workspace)
    db.session.flush()
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role, status="active"))
    db.session.commit()
    return workspace


def make_experience(user, role="owner", name="Gate J Experience"):
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


def _frame_bytes():
    image = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(image)
    for index in range(0, 320, 24):
        draw.line((index, 0, 320 - index // 2, 239), fill="black", width=2)
    draw.rectangle((70, 50, 250, 190), outline="black", width=5)
    output = BytesIO()
    image.save(output, format="JPEG")
    output.seek(0)
    return output


@pytest.mark.parametrize("session_count", [1, 5, 10, 20])
def test_gate_j_concurrent_session_api_rehearsal_is_bounded(client, project_with_pair, session_count):
    project, _pair = project_with_pair
    status_codes = []
    for index in range(session_count):
        response = client.post(
            "/detect_init",
            data={
                "project_id": str(project.id),
                "scan_session_id": f"gate-j-session-{index}",
                "test_image": (_frame_bytes(), f"frame-{index}.jpg"),
            },
            content_type="multipart/form-data",
        )
        status_codes.append(response.status_code)
        payload = response.get_json()
        assert payload["detected"] is False
        assert "scan_session_id" not in payload or payload["scan_session_id"] is None
    assert status_codes == [200] * session_count


def test_gate_j_runtime_modes_have_certification_bounds():
    full = mode_config("full")
    standard = mode_config("standard")
    lightweight = mode_config("lightweight")
    fallback = mode_config("fallback")
    assert full["frame_width"] > standard["frame_width"] > lightweight["frame_width"] > fallback["frame_width"]
    assert full["detect_interval_ms"] < standard["detect_interval_ms"] < lightweight["detect_interval_ms"]
    assert select_runtime_mode({"secure_context": True, "camera_api": True, "webassembly": True, "canvas": True, "device_memory": 1, "hardware_concurrency": 2}) == "lightweight"
    assert select_runtime_mode({"secure_context": False, "camera_api": True, "webassembly": True, "canvas": True}) == "fallback"


def test_gate_j_target_loss_reacquisition_path_and_request_bounds():
    machine = ScannerStateMachine()
    for state in ["loading_shell", "checking_capabilities", "requesting_camera", "initializing_camera", "loading_opencv", "loading_wasm", "initializing_scanner", "ready_to_scan", "detecting", "tracking"]:
        machine.transition(state)
    machine.transition("target_lost")
    machine.transition("recovering")
    machine.transition("detecting")
    machine.transition("tracking")
    assert machine.state == "tracking"

    policy = RecognitionRequestPolicy("lightweight")
    first = policy.start(0)
    assert policy.can_start(100, page_visible=True, camera_active=True) is False
    assert policy.finish(first + 1) == "stale"
    assert policy.finish(first) == "accepted"
    assert policy.can_start(650, page_visible=True, camera_active=True) is True


def test_gate_j_same_qr_update_rollback_and_draft_isolation(client, normal_user, gate_j_enabled):
    experience = make_experience(normal_user)
    trigger, _image, video_a, _artifact = add_ready_trigger(experience, video_name="gate-j-video-a.mp4")
    version1 = publish_ready_experience(experience, normal_user, "gate-j-publish-v1")
    destination = f"/e/{experience.public_key}"
    assert "gate-j-video-a.mp4" in client.get(destination).get_data(as_text=True)

    video_b = add_asset(experience.workspace, "video", "gate-j-video-b.mp4")
    from models import TriggerAsset, ExperienceVersionTrigger, db

    db.session.add(TriggerAsset(trigger_id=trigger.id, asset_id=video_b.id, role="video"))
    db.session.commit()
    draft_html = client.get(destination).get_data(as_text=True)
    assert "gate-j-video-a.mp4" in draft_html
    assert "gate-j-video-b.mp4" not in draft_html

    version2 = publish_ready_experience(experience, normal_user, "gate-j-publish-v2")
    assert f"/e/{experience.public_key}" == destination
    published_html = client.get(destination).get_data(as_text=True)
    assert "Published Version 2" in published_html
    assert "gate-j-video-b.mp4" in published_html
    assert ExperienceVersionTrigger.query.filter_by(experience_version_id=version1.id).one().video_asset_id == video_a.id
    assert ExperienceVersionTrigger.query.filter_by(experience_version_id=version2.id).one().video_asset_id == video_b.id

    from publishing import rollback_experience_to_version

    rollback_experience_to_version(experience.id, version1.id, normal_user.id, "gate-j-rollback-v1")
    rollback_html = client.get(destination).get_data(as_text=True)
    assert "Published Version 1" in rollback_html
    assert "gate-j-video-a.mp4" in rollback_html


def test_gate_j_public_viewer_session_is_stable_per_session_and_not_sequential(app, normal_user, gate_j_enabled):
    experience = make_experience(normal_user)
    add_ready_trigger(experience, video_name="gate-j-session-video.mp4")
    publish_ready_experience(experience, normal_user, "gate-j-session-publish")
    destination = f"/e/{experience.public_key}"

    with app.test_client() as first_client:
        first_html = first_client.get(destination).get_data(as_text=True)
        repeat_html = first_client.get(destination).get_data(as_text=True)
    with app.test_client() as second_client:
        second_html = second_client.get(destination).get_data(as_text=True)

    def session_id(html):
        marker = 'data-viewer-session="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    first_id = session_id(first_html)
    assert len(first_id) == 32
    assert session_id(repeat_html) == first_id
    assert session_id(second_html) != first_id


def test_gate_j_browser_probe_exercises_runtime_contract():
    from pathlib import Path

    probe = Path("gate-j/browser-probe.html").read_text(encoding="utf-8")
    assert "scanner-runtime.js" in probe
    assert "createStateMachine" in probe
    assert "selectRuntimeMode" in probe
    assert "getUserMedia" in probe
    assert "__GATE_J_DONE__" in probe
