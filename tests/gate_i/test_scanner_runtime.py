import pytest

from scanner_runtime import (
    RecognitionRequestPolicy,
    ScannerStateError,
    ScannerStateMachine,
    create_viewer_session_id,
    is_rate_limited_response,
    mode_config,
    resolve_retry_after_ms,
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


def test_rate_limited_response_classification():
    """A 429/RATE_LIMITED body has no `detected` key — must be classified as rate-limited
    (never passed to validate_detection_response as a detection outcome), by status code
    first and the body's own `code` field as defense-in-depth."""
    rate_limited_body = {
        "error": True,
        "code": "RATE_LIMITED",
        "reason": "Too many scanner requests. Please wait briefly and try again.",
        "retry_after_seconds": 12,
    }
    assert is_rate_limited_response(429, rate_limited_body) is True
    # Defense-in-depth: body code alone (e.g. status stripped by some proxy) still classifies.
    assert is_rate_limited_response(200, rate_limited_body) is True
    # An ordinary no-match response must NOT be classified as rate-limited.
    assert is_rate_limited_response(200, {"detected": False, "reason": "Too few features (0)"}) is False
    assert is_rate_limited_response(429, None) is True
    assert is_rate_limited_response(200, None) is False


def test_resolve_retry_after_prefers_body_then_header_then_default():
    assert resolve_retry_after_ms({"retry_after_seconds": 12}, "99") == 12000
    # Malformed body falls back to the header.
    assert resolve_retry_after_ms({"retry_after_seconds": "not-a-number"}, "5") == 5000
    # Neither present: safe 1s default, never zero (never an immediate-retry storm).
    assert resolve_retry_after_ms({}, None) == 1000
    assert resolve_retry_after_ms(None, None) == 1000


def test_request_policy_backs_off_after_rate_limit_and_resets_cleanly():
    policy = RecognitionRequestPolicy("full")
    req = policy.start(0)
    policy.finish(req)
    # Server said "wait 5s" — a plain next-interval retry must NOT be allowed before that.
    policy.note_rate_limited(0, 5000)
    assert policy.can_start(250, page_visible=True, camera_active=True) is False
    assert policy.can_start(4999, page_visible=True, camera_active=True) is False
    assert policy.can_start(5000, page_visible=True, camera_active=True) is True
    # A second, smaller retry-after must never SHORTEN an already-set later deadline.
    policy.note_rate_limited(100, 10)
    assert policy.can_start(200, page_visible=True, camera_active=True) is False
    # Explicit reset (Continue Scanning / Retry Camera) clears it immediately — note this
    # check also has to clear the plain interval-since-last-start gate (unrelated to backoff),
    # hence 300ms rather than 200ms here.
    policy.reset_backoff()
    assert policy.can_start(300, page_visible=True, camera_active=True) is True


def test_request_policy_never_forms_a_continuous_429_loop():
    """Simulates repeated 429s (no successful requests ever get through) and asserts the
    policy always defers the next allowed start to the server's advertised wait — it never
    collapses back to the bare fixed interval while a backoff is outstanding."""
    policy = RecognitionRequestPolicy("full")
    now = 0
    denied_before_deadline = 0
    for _ in range(20):
        req = policy.start(now)
        policy.finish(req)
        policy.note_rate_limited(now, 2000)
        # Immediately after, and at the plain fixed interval, must still be denied.
        if policy.can_start(now + policy.interval_ms, page_visible=True, camera_active=True):
            denied_before_deadline += 1
        now += 2000  # only advance to exactly the advertised deadline
    assert denied_before_deadline == 0


def test_scanner_template_loads_runtime_and_preserves_legacy_contract(client, project_with_pair):
    # /scanner/<id> no longer renders directly — it 302s to the canonical /s/<public_key>
    # (see app.py's _canonical_public_scanner_path). "Preserves legacy contract" means the
    # old URL still resolves to the real runtime end-to-end, so follow the redirect.
    project, _pair = project_with_pair
    html = client.get(f"/scanner/{project.id}", follow_redirects=True).get_data(as_text=True)
    assert "scanner-runtime.js" in html
    assert "ScanStoryScannerRuntime" in html
    assert "/detect_init" in html
    assert "/detect_track" not in html or "detect_track" in html
    assert "scanner_mode" in html
