import io

import pytest


pytestmark = pytest.mark.contract


def test_detect_init_missing_payload_contract(client):
    response = client.post("/detect_init", data={})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["detected"] is False
    assert "reason" in payload


def test_detect_init_invalid_project_contract(client):
    response = client.post(
        "/detect_init",
        data={"project_id": "99999", "test_image": (io.BytesIO(b"not-image"), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["detected"] is False
    assert payload["reason"] == "Project not found"


def test_detect_init_invalid_image_contract(client, project_with_pair):
    project, pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={
            "project_id": str(project.id),
            "scan_session_id": "session-1",
            "test_image": (io.BytesIO(b"not-image"), "frame.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["detected"] is False
    assert payload["reason"] == "Invalid image"


def test_detect_track_missing_payload_contract(client):
    response = client.post("/detect_track", data={})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "reason" in payload


def test_session_end_missing_payload_contract(client):
    response = client.post("/api/scanner/session/end", json={})
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_scanner_session_end_counts_success_once(client, app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    scan = app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id,
        user_id=normal_user.id,
        scan_session_id="session-count",
        is_successful=True,
        counted=False,
    )
    db_session.add(scan)
    db_session.commit()
    first = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "session-count"})
    second = client.post("/api/scanner/session/end", json={"project_id": project.id, "session_id": "session-count"})
    assert first.get_json()["counted"] is True
    assert second.get_json()["counted"] is False
    assert app_module.User.query.get(normal_user.id).scans_used == 1
