import pytest

from scanner_runtime import (
    RecognitionRequestPolicy,
    ScannerStateError,
    ScannerStateMachine,
    create_viewer_session_id,
    mode_config,
    select_runtime_mode,
    validate_detection_response,
    viewer_error,
)


def test_state_machine_valid_invalid_timeout_and_duplicate_init():
    machine = ScannerStateMachine()
    assert machine.transition("loading_shell", now_ms=0) == "loading_shell"
    assert machine.transition("checking_capabilities", now_ms=100) == "checking_capabilities"
    assert machine.transition("requesting_camera", now_ms=200) == "requesting_camera"
    assert machine.timed_out(16000) is True
    with pytest.raises(ScannerStateError):
        machine.transition("tracking", now_ms=300)
    machine.transition("fallback", now_ms=400)
    assert machine.state == "fallback"

    duplicate = ScannerStateMachine()
    duplicate.transition("loading_shell")
    duplicate.transition("failed")
    duplicate.transition("fallback")
    with pytest.raises(ScannerStateError):
        duplicate.transition("loading_shell")


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({"secure_context": False, "camera_api": True, "webassembly": True, "canvas": True}, "fallback"),
        ({"secure_context": True, "camera_api": False, "webassembly": True, "canvas": True}, "fallback"),
        ({"secure_context": True, "camera_api": True, "webassembly": False, "canvas": True}, "fallback"),
        ({"secure_context": True, "camera_api": True, "webassembly": True, "canvas": True, "device_memory": 1, "hardware_concurrency": 2}, "lightweight"),
        ({"secure_context": True, "camera_api": True, "webassembly": True, "canvas": True, "device_memory": 4, "hardware_concurrency": 4, "screen_width": 390}, "standard"),
        ({"secure_context": True, "camera_api": True, "webassembly": True, "canvas": True, "webgl": True, "device_memory": 8, "hardware_concurrency": 8, "screen_width": 1080}, "full"),
    ],
)
def test_runtime_mode_selection(capabilities, expected):
    assert select_runtime_mode(capabilities) == expected
    assert select_runtime_mode(capabilities, override="lightweight") == "lightweight"
    assert select_runtime_mode(capabilities, prior_failure=True) == "fallback"


def test_mode_configs_are_bounded():
    assert mode_config("full")["frame_width"] <= 960
    assert mode_config("standard")["detect_interval_ms"] >= 300
    assert mode_config("lightweight")["tracking_points"] <= 90
    assert mode_config("fallback")["frame_width"] == 0


def test_recognition_request_policy_blocks_overlap_stale_and_timeout():
    policy = RecognitionRequestPolicy("standard")
    assert policy.can_start(0, page_visible=True, camera_active=True) is True
    req1 = policy.start(0)
    assert policy.can_start(400, page_visible=True, camera_active=True) is False
    assert policy.finish(req1 + 1) == "stale"
    assert policy.finish(req1) == "accepted"
    assert policy.can_start(100, page_visible=True, camera_active=True) is False
    assert policy.can_start(400, page_visible=False, camera_active=True) is False
    assert policy.can_start(400, page_visible=True, camera_active=False) is False
    req2 = policy.start(400)
    assert policy.timed_out(9000) is True
    assert policy.finish(req2) == "accepted"


def test_detection_response_validation_and_viewer_errors():
    assert validate_detection_response({"detected": False}) == (True, "NO_MATCH")
    ok, code = validate_detection_response(
        {
            "detected": True,
            "video_url": "/video/1/0",
            "corners": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}],
        }
    )
    assert ok is True and code == "MATCH"
    assert validate_detection_response({"detected": True, "corners": []}) == (False, "INVALID_DETECTION_RESPONSE")
    assert viewer_error("CAMERA_PERMISSION_DENIED")["fallback_allowed"] is True
    assert viewer_error("EXPERIENCE_ARCHIVED")["retry_allowed"] is False


def test_viewer_session_id_is_unpredictable_shape():
    first = create_viewer_session_id()
    second = create_viewer_session_id()
    assert len(first) == 32
    assert first != second


def test_scanner_template_loads_runtime_and_preserves_legacy_contract(client, project_with_pair):
    project, _pair = project_with_pair
    html = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    assert "scanner-runtime.js" in html
    assert "ScanStoryScannerRuntime" in html
    assert "/detect_init" in html
    assert "/detect_track" not in html or "detect_track" in html
    assert "scanner_mode" in html
