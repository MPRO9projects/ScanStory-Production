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


# ---------------------------------------------------------------------
# OpenCV cold-start telemetry (Fix 4, V1 Agent 2) - lightweight, low-cardinality sink.
# Server-side contract only: outcome enum, best-effort behavior on missing/suspended
# project, and that submitted numeric/boolean fields land in the structured log record
# (captured via caplog rather than a mock, since app.logger.info(..., extra=...) is the
# actual delivery mechanism this endpoint promises).
# ---------------------------------------------------------------------

def test_opencv_telemetry_rejects_missing_body(client, project_with_pair):
    project, _pair = project_with_pair
    resp = client.post(f"/api/scanner/{project.id}/opencv-telemetry", data={})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_opencv_telemetry_rejects_invalid_outcome(client, project_with_pair):
    project, _pair = project_with_pair
    resp = client.post(f"/api/scanner/{project.id}/opencv-telemetry", data={"outcome": "not_a_real_outcome"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_opencv_telemetry_missing_project_is_best_effort_ok(client):
    resp = client.post("/api/scanner/999999/opencv-telemetry", data={"outcome": "first_attempt_success"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "logged": False}


def test_opencv_telemetry_suspended_project_is_best_effort_ok(client, db_session, project_with_pair):
    project, _pair = project_with_pair
    project.is_active = False
    db_session.commit()
    resp = client.post(f"/api/scanner/{project.id}/opencv-telemetry", data={"outcome": "terminal_failure"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "logged": False}


@pytest.mark.parametrize(
    "outcome", ["first_attempt_success", "user_retry_success", "first_attempt_failure", "terminal_failure"]
)
def test_opencv_telemetry_logs_structured_outcome(client, project_with_pair, caplog, outcome):
    project, _pair = project_with_pair
    with caplog.at_level("INFO"):
        resp = client.post(
            f"/api/scanner/{project.id}/opencv-telemetry",
            data={
                "outcome": outcome,
                "attempt_count": "3",
                "total_duration_ms": "12345",
                "sw_controller": "false",
                "device_memory": "4",
                "hardware_concurrency": "8",
                "connection_effective_type": "4g",
                "scan_session_id": "sess-1",
            },
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "logged": True}
    records = [r for r in caplog.records if getattr(r, "scanner_opencv_telemetry", None)]
    assert len(records) == 1
    logged = records[0].scanner_opencv_telemetry
    assert logged["outcome"] == outcome
    assert logged["attempt_count"] == 3
    assert logged["total_duration_ms"] == 12345
    # form-encoded "false" must parse as boolean False, never truthy-because-non-empty-string.
    assert logged["sw_controller"] is False
    assert logged["device_memory"] == 4
    assert logged["hardware_concurrency"] == 8
    assert logged["connection_effective_type"] == "4g"
    assert logged["scan_session_id"] == "sess-1"


def test_opencv_telemetry_rate_limited_after_burst(client, project_with_pair):
    project, _pair = project_with_pair
    last = None
    for _ in range(35):
        last = client.post(f"/api/scanner/{project.id}/opencv-telemetry", data={"outcome": "terminal_failure"})
    assert last.status_code == 429
