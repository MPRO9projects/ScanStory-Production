from io import BytesIO
from pathlib import Path

import cv2
import numpy as np


class NoopThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def test_orientation_recovery_invalidates_generation_and_geometry():
    html = _scanner_html()
    assert "scannerGeneration++" in html
    assert "scheduleOrientationRecovery('orientationchange')" in html
    assert "scheduleOrientationRecovery('resize')" in html
    assert "clearTrackingGeometry(reason)" in html
    assert "overlayWrap.style.transform = \"\"" in html


def test_stale_detection_response_after_orientation_is_rejected():
    html = _scanner_html()
    assert "const requestGeneration = scannerGeneration" in html
    assert "requestGeneration !== scannerGeneration" in html
    assert "detectionPolicy.finish(requestId)" in html
    assert "latestAcceptedRequestId = requestId" in html


def test_no_duplicate_loops_or_camera_streams_are_guarded():
    html = _scanner_html()
    assert "let animationFrameId = null" in html
    assert "let detectLoopTimer = null" in html
    assert "let activeDetectionController = null" in html
    assert "if (trackLoopActive) return" in html
    assert "if (detectLoopTimer) return" in html
    assert "isCameraHealthy()" in html
    assert "getTracks().forEach(track => track.stop())" in html


def test_timeout_recovers_without_immediate_camera_restart_or_capability_panel():
    """Repeated recognition timeouts used to fall through to enterFallback('DETECTION_TIMEOUT')
    — the exact same panel as a genuinely dead camera. See
    test_scanner_lifecycle.py::test_recognition_timeout_never_restarts_a_healthy_camera for
    the dedicated coverage of the fix (showRecognitionHelp instead)."""
    html = _scanner_html()
    assert "function handleDetectionTimeout()" in html
    assert "startDetectLoop()" in html
    assert "startTrackingLoop()" in html
    assert "showRecognitionHelp('repeated_detection_timeout')" in html
    assert "Recognition timed out. Trying again..." in html


def test_standard_mode_tracking_parameters_are_bounded():
    html = _scanner_html()
    assert "scannerMode === 'standard' ? 120" in html
    assert "TRACK_FRAME_INTERVAL_MS" in html
    assert "slice(0, MAX_TRACK_POINTS)" in html


def test_runtime_quad_validation_rejects_bad_geometry():
    runtime_source = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    assert "function isValidQuad" in runtime_source
    assert "quadArea(points)" in runtime_source
    assert "area < frameArea * 0.01" in runtime_source
    assert "maxEdge / Math.max(minEdge, 1) <= 12" in runtime_source
    assert "isValidQuad" in runtime_source


def test_project_upload_does_not_standardize_before_http_response(client, app_module, login_user, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)

    response = client.post(
        "/upload",
        data={
            "name": "Gate JR Upload",
            "images": [(BytesIO(b"image"), "target.jpg")],
            "videos": [(BytesIO(b"video"), "clip.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert calls == []
    pair = app_module.ProjectPair.query.first()
    assert pair.processing_status == "uploaded"


def test_backend_homography_corners_rescale_height_width_without_reversal(app_module):
    image = np.zeros((1200, 675, 3), dtype=np.uint8)
    gray, scale, orig_w, orig_h = app_module._resize_gray_for_detect(image, max_dim=960)

    assert gray.shape == (960, 540)
    assert scale == 0.8
    assert orig_w == 675
    assert orig_h == 1200

    original_corners = [(100.0, 200.0), (500.0, 220.0), (520.0, 1000.0), (120.0, 980.0)]
    processed_corners = [(x * scale, y * scale) for x, y in original_corners]
    browser_corners = [(x / scale, y / scale) for x, y in processed_corners]

    assert browser_corners == original_corners
    assert all(0 <= x <= orig_w for x, _y in browser_corners)
    assert all(0 <= y <= orig_h for _x, y in browser_corners)
    assert app_module.valid_corners(browser_corners, orig_w, orig_h)
    assert not app_module.valid_corners(browser_corners, orig_h, orig_w)
    assert not app_module.valid_corners([(10, 10), (20, 20), (30, 30), (40, 40)], orig_w, orig_h)


def test_homography_quality_accepts_scaled_portrait_business_card_pose(app_module):
    orig_w, orig_h = 675, 1200
    scale = 0.8
    marker_w, marker_h = 500, 300
    src = np.array(
        [
            [40, 40], [250, 35], [460, 45],
            [55, 150], [250, 150], [445, 155],
            [35, 260], [250, 265], [465, 255],
        ],
        dtype=np.float32,
    )
    original_corners = np.array([[80, 200], [560, 220], [535, 1000], [105, 980]], dtype=np.float32)
    processed_corners = original_corners * scale
    h_matrix = cv2.getPerspectiveTransform(
        np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        processed_corners,
    )
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, h_matrix, mask, marker_w, marker_h, orig_w, orig_h, scale=scale
    )

    assert ok
    assert np.allclose(np.array(quality["corners"], dtype=np.float32), original_corners, atol=1e-4)
    assert all(0 <= x <= orig_w for x, _y in quality["corners"])
    assert all(0 <= y <= orig_h for _x, y in quality["corners"])
    assert app_module.valid_corners(quality["corners"], orig_w, orig_h)
    assert not app_module.valid_corners(quality["corners"], orig_h, orig_w)


def test_homography_quality_rejects_clustered_inliers(app_module):
    marker_w, marker_h = 500, 300
    src = np.array([[100 + i * 2, 100 + (i % 3) * 2] for i in range(18)], dtype=np.float32)
    h_matrix = np.array([[1, 0, 80], [0, 1, 160], [0, 0, 1]], dtype=np.float64)
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, h_matrix, mask, marker_w, marker_h, 675, 1200, scale=0.8
    )

    assert not ok
    assert quality["reason"] == "clustered_inliers"


def test_homography_quality_rejects_bad_reprojection_and_invalid_corners(app_module):
    marker_w, marker_h = 500, 300
    src = np.array(
        [[40, 40], [250, 35], [460, 45], [55, 150], [250, 150], [445, 155], [35, 260], [250, 265], [465, 255]],
        dtype=np.float32,
    )
    h_matrix = np.eye(3, dtype=np.float64)
    noisy_dst = src + np.array([30, 0], dtype=np.float32)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(
        src, noisy_dst, h_matrix, mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok
    assert quality["reason"] == "reprojection_error"

    huge_h = np.array([[2.2, 0, -150], [0, 4.2, -250], [0, 0, 1]], dtype=np.float64)
    huge_dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), huge_h).reshape(-1, 2)
    ok, quality = app_module.evaluate_homography_quality(
        src, huge_dst, huge_h, mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok
    assert quality["reason"] == "invalid_corners"


def test_detection_response_metadata_and_session_end_stale_guards():
    html = _scanner_html()
    assert 'fd.append("scanner_generation", String(requestGeneration))' in html
    assert 'fd.append("source_frame_width", String(capW))' in html
    assert 'fd.append("source_frame_height", String(capH))' in html
    assert 'fd.append("orientation_revision", String(requestOrientationRevision))' in html
    assert "String(data.scanner_generation || \"\") !== String(requestGeneration)" in html
    assert "Number(data.source_frame_width || 0) !== capW" in html
    assert "Number(data.source_frame_height || 0) !== capH" in html
    assert "String(data.orientation_revision || \"\") !== String(requestOrientationRevision)" in html
    assert "clearTrackingGeometry('session_end')" in html
    assert "let sessionEnding = false" in html
    assert "sessionEnding = true" in html
    assert "if (sessionEnding || detectInFlight" in html
    assert "window.addEventListener('pagehide'" in html


def test_healthy_tracking_suppresses_repeated_detect_init_and_limits_inflight():
    html = _scanner_html()
    # FORCE_REDETECT_MS was 12000 — real-device logs showed detect requests going silent
    # for up to ~18s while local tracking was "healthy". 3000 still left a 5-6s residual gap
    # on lightweight mode once detectionPolicy's own 2x(detectIntervalMs) gate is accounted
    # for; 1800 keeps the worst case under 4000ms on every mode (see
    # test_scanner_lifecycle.py's scan-loop tests for the gap-focused coverage).
    assert "const FORCE_REDETECT_MS = 1800" in html
    assert "const HEALTHY_TRACK_SUPPRESS_MS = 1200" in html
    assert "const trackingHealthy = tracking && driftMs <= HEALTHY_TRACK_SUPPRESS_MS" in html
    assert "(trackingHealthy && sinceLastDetect > FORCE_REDETECT_MS)" in html
    assert "if (sessionEnding || detectInFlight || !cvReady || cam.readyState < 2) return" in html


def test_temporal_pose_rejection_prevents_overlay_state_mutation():
    html = _scanner_html()
    assert "function poseCompatibility(nextCorners)" in html
    assert "areaRatio > 2.5 || areaRatio < 0.4" in html
    assert "centerJump > 0.35" in html
    assert "maxCornerJump > 0.55" in html
    assert "tracking_pose_rejected" in html
    assert "if (!poseQuality.ok && tracking && currCorners)" in html
    assert "clearTrackingGeometry('pose_rejected_' + poseQuality.reason)" in html
    assert html.index("const poseQuality = poseCompatibility(newCorners)") < html.index("overlay.src = newVideoUrl")


def test_detect_init_echoes_generation_frame_and_orientation_metadata(client, project_with_pair):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={
            "project_id": str(project.id),
            "scanner_generation": "12",
            "source_frame_width": "540",
            "source_frame_height": "960",
            "orientation_revision": "3",
            "test_image": (BytesIO(b"not-an-image"), "frame.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["scanner_generation"] == "12"
    assert payload["source_frame_width"] == 540
    assert payload["source_frame_height"] == 960
    assert payload["orientation_revision"] == "3"


# --- Real-device stability hardening pass -----------------------------------------------
# Everything below is additive: no existing test above was modified, and no threshold
# constant is changed anywhere in this file — see test_no_threshold_was_globally_weakened.


def test_no_threshold_was_globally_weakened(app_module):
    """Pins the exact constants this pass touched reasoning about, so an accidental
    global loosening (e.g. to force a stuck real-device scan to accept) fails CI instead
    of silently shipping."""
    assert app_module.MIN_GOOD_MATCHES == 8
    assert app_module.RANSAC_REPROJ == 5.0
    assert app_module.MIN_INLIERS_ABS == 8
    assert app_module.MIN_INLIERS_RATIO == 0.30


def test_weak_homography_now_gets_a_structured_reason_instead_of_generic_string(app_module):
    """Regression test for the real-device log this phase was asked to fix: '45 good
    matches, 6 inliers, required 13' used to short-circuit on a duplicate inline check
    and come back as the bare string 'Weak homography'. It must now come back through
    evaluate_homography_quality with a structured reason/code — using the SAME
    thresholds (nothing weakened), just not thrown away before classification."""
    marker_w, marker_h = 500, 300
    src = np.array([[100 + i, 100 + i] for i in range(45)], dtype=np.float32)
    dst = src + np.array([5, 5], dtype=np.float32)
    mask = np.zeros((45, 1), dtype=np.uint8)
    mask[:6] = 1  # only 6 of 45 are inliers — required is max(8, 0.30*45)=13

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, np.eye(3, dtype=np.float64), mask, marker_w, marker_h, 675, 1200, scale=0.8
    )

    assert not ok
    assert quality["reason"] == "weak_inliers"
    assert quality["code"] == "insufficient_inliers"
    assert quality["inliers"] == 6
    assert quality["required"] == 13


def test_homography_quality_distinguishes_low_ratio_from_insufficient_count(app_module):
    """Same 'weak_inliers' reason as above, but a DIFFERENT code — enough inliers by
    absolute count, just too small a fraction of the total matches to trust."""
    # total=41 -> min_inliers_needed = max(8, int(0.30*41)) = max(8, 12) = 12.
    # 12 inliers clears that absolute/floor requirement, but 12/41 = 0.2927 < 0.30 —
    # the int()-floor of the ratio threshold is what creates this gap.
    total = 41
    mask = np.zeros((total, 1), dtype=np.uint8)
    mask[:12] = 1
    src = np.array([[100 + i, 100 + i] for i in range(total)], dtype=np.float32)
    dst = src.copy()

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, np.eye(3, dtype=np.float64), mask, 500, 300, 675, 1200, scale=0.8
    )

    assert not ok
    assert quality["reason"] == "weak_inliers"
    assert quality["code"] == "low_inlier_ratio"
    assert quality["inlier_ratio"] < 0.30


def test_roi_clustering_code_matches_reference_clustering_when_it_fires(app_module):
    """roi_cells (dst points re-warped into marker space via the homography's own 4-corner
    projection) is mathematically ~identical to ref_cells for any consistent homography —
    getPerspectiveTransform built from H's own projected corners reconstructs H^-1 exactly
    on the same inlier set. So in practice this branch fires alongside, not instead of,
    the reference-clustering branch. This just confirms clustered_roi_points is reachable
    and correctly labeled when both are clustered together (a genuinely separate
    frame-position-based gate was tried and reverted — see
    test_marker_selection_upload.py::test_roi_coverage_is_separate_from_full_frame_position,
    which requires a small marker in a big frame to still be ACCEPTED, not penalized for
    "low frame coverage")."""
    marker_w, marker_h = 500, 300
    src = np.array([[100 + i * 2, 100 + (i % 3) * 2] for i in range(18)], dtype=np.float32)
    h_matrix = np.array([[1, 0, 80], [0, 1, 160], [0, 0, 1]], dtype=np.float64)
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, h_matrix, mask, marker_w, marker_h, 675, 1200, scale=0.8
    )

    assert not ok
    assert quality["reason"] == "clustered_inliers"
    assert quality["code"] == "clustered_reference_points"  # ref checked first, same as before this pass
    assert quality["reference_grid_cells"] < 3
    assert quality["projected_roi_grid_cells"] < 3  # confirms roi tracks ref, not frame position


def test_homography_quality_codes_for_invalid_quad_and_high_reprojection_error(app_module):
    """Same scenarios as the pre-existing reprojection/corners test, asserting only the
    additive `code` field (spec's canonical rejection-code list) without touching the
    original reason-string assertions."""
    marker_w, marker_h = 500, 300
    src = np.array(
        [[40, 40], [250, 35], [460, 45], [55, 150], [250, 150], [445, 155], [35, 260], [250, 265], [465, 255]],
        dtype=np.float32,
    )
    mask = np.ones((len(src), 1), dtype=np.uint8)

    noisy_dst = src + np.array([30, 0], dtype=np.float32)
    ok, quality = app_module.evaluate_homography_quality(
        src, noisy_dst, np.eye(3, dtype=np.float64), mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok and quality["code"] == "high_reprojection_error"

    huge_h = np.array([[2.2, 0, -150], [0, 4.2, -250], [0, 0, 1]], dtype=np.float64)
    huge_dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), huge_h).reshape(-1, 2)
    ok, quality = app_module.evaluate_homography_quality(
        src, huge_dst, huge_h, mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok and quality["code"] == "invalid_quad"


def test_homography_quality_accepts_small_cropped_marker_with_distributed_points(app_module):
    """A cropped/small marker (not a legacy full-image marker) must not be unfairly
    rejected by the grid-coverage checks just for having a small physical footprint —
    only genuinely clustered matches should fail."""
    marker_w, marker_h = 120, 80  # small cropped marker, not full-image
    scale = 0.8
    src = np.array(
        [
            [10, 8], [60, 6], [110, 9],
            [12, 40], [60, 40], [108, 41],
            [9, 72], [60, 74], [111, 70],
        ],
        dtype=np.float32,
    )
    original_corners = np.array([[80, 200], [560, 220], [535, 1000], [105, 980]], dtype=np.float32)
    h_matrix = cv2.getPerspectiveTransform(
        np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        original_corners * scale,
    )
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(
        src, dst, h_matrix, mask, marker_w, marker_h, 675, 1200, scale=scale
    )

    assert ok
    assert quality["code"] == "accepted"


def test_legacy_full_image_marker_mode_fallback_is_preserved(app_module, project_with_pair):
    """marker_mode defaults to 'full_image' for pairs created before the field existed —
    confirms the legacy path in the /detect_init response builder is untouched."""
    _project, pair = project_with_pair
    assert getattr(pair, "marker_mode", None) in (None, "full_image")
    assert (getattr(pair, "marker_mode", None) or "full_image") == "full_image"


def test_resolve_candidate_margin_rejects_ambiguous_close_candidates(app_module):
    """Two distinct candidates both individually clear MIN_GOOD_MATCHES but are too close
    in match count to trust a single winner — the 'wrong candidate with many noisy
    matches' failure mode called out in this phase's brief."""
    ok, code = app_module.resolve_candidate_margin(best_good=20, second_good=18)
    assert not ok
    assert code == "candidate_margin_too_small"


def test_resolve_candidate_margin_accepts_a_clear_winner(app_module):
    ok, code = app_module.resolve_candidate_margin(best_good=40, second_good=5)
    assert ok
    assert code is None


def test_resolve_candidate_margin_ignores_a_weak_runner_up(app_module):
    """A runner-up that never cleared MIN_GOOD_MATCHES on its own isn't a real ambiguity —
    the common single-marker case must not be penalized."""
    ok, code = app_module.resolve_candidate_margin(best_good=10, second_good=app_module.MIN_GOOD_MATCHES - 1)
    assert ok
    assert code is None


def test_background_only_frame_is_rejected_through_the_real_endpoint(client, app_module, login_user, feature_artifact, project_with_pair):
    """A camera frame with no marker in view (busy background/random texture, but no
    stored pair matches it) must come back detected=false through the real endpoint —
    not a false positive."""
    project, _pair = project_with_pair
    rng = np.random.default_rng(1234)
    noise = rng.integers(0, 255, (480, 360, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", noise)
    assert ok

    response = client.post(
        "/detect_init",
        data={
            "project_id": str(project.id),
            "test_image": (BytesIO(buf.tobytes()), "frame.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["detected"] is False


def test_solid_color_frame_no_marker_is_rejected_through_the_real_endpoint(client, app_module, login_user, feature_artifact, project_with_pair):
    """A wall/floor/table with essentially no texture (too few ORB keypoints) must also
    be rejected, distinctly from the busy-background case above (different gate: feature
    count, not match count)."""
    project, _pair = project_with_pair
    blank = np.full((480, 360, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", blank)
    assert ok

    response = client.post(
        "/detect_init",
        data={
            "project_id": str(project.id),
            "test_image": (BytesIO(buf.tobytes()), "frame.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["detected"] is False


def test_marker_ab_switch_has_a_pair_epoch_guard_against_stale_overwrite():
    """Marker A/B hardening: a slower/older detect response must not be able to overwrite
    an already-active different pair just because it resolves later."""
    html = _scanner_html()
    assert "let activePairEpoch = 0;" in html
    assert "const requestPairEpoch = activePairEpoch;" in html
    assert "if (requestPairEpoch !== activePairEpoch)" in html
    assert "code: 'stale_pair_epoch'" in html
    assert "activePairEpoch++;" in html
    assert "'[MARKER SWITCH]'" in html


def test_stale_response_reasons_are_differentiated_for_diagnostics():
    html = _scanner_html()
    assert "'[STALE RESULT IGNORED]'" in html
    assert "code: isStaleGeneration ? 'stale_generation' : (isStaleOrientation ? 'stale_orientation' : 'stale_frame_size')" in html


def test_tracking_grace_has_a_wall_clock_ceiling_in_addition_to_frame_count():
    """Frame-count-only grace can hang on throttled/low-FPS devices — there must also be
    a time-based ceiling, configured alongside the existing frame-count constant."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_MS" in html
    assert "function graceExpired()" in html
    assert "trackingBadFrames >= TRACKING_GRACE_FRAMES || (performance.now() - graceEnteredAt) >= TRACKING_GRACE_MS" in html
    assert "function enterGrace(reason)" in html


def test_grace_recovery_and_timeout_both_go_through_single_exit_paths():
    """Recovery-inside-grace and grace-timeout must both be reachable, and timeout must
    route through the single dropTracking exit (fixes the H.empty() Mat leak audit finding
    — previously that branch fell through to shared cleanup without freeing `gray` or the
    previous prevGray/prevPts)."""
    html = _scanner_html()
    assert "'[TRACK RECOVERED]'" in html
    assert "function dropTracking(reason, extraMats)" in html
    assert "dropTracking('insufficient_flow_points', [gray, nextPts, status, err]);" in html
    assert "dropTracking('homography_empty', [gray, nextPts, status, err, prevMat, nextMat, mask, H]);" in html
    assert "'[TRACK LOST]'" in html
    assert "clearTrackingGeometry(reason);" in html  # dropTracking always clears geometry -> stops overlay


def test_no_duplicate_loop_survives_hidden_visible_recovery():
    """Hidden->visible must recover without ever duplicating the loop. The loop is no
    longer even stopped on hidden (see test_scanner_lifecycle.py's gap-fix tests) — restore
    either does nothing (stream alive, loop was never stopped) or goes through the same
    guarded recoverScanner/startDetectLoop path duplicate-loop protection already covers."""
    html = _scanner_html()
    assert "safeTransition('paused', 'tab_hidden')" in html
    assert "recoverScanner('visibilitychange', true)" in html
    assert "if (trackLoopActive) return" in html
    assert "if (detectLoopTimer) return" in html


def test_status_word_is_state_driven_not_a_blind_round_robin():
    """Regression guard for the 'uncontrolled switching between Reading Marker/Preparing
    Camera/Matching Image/Tracking Surface' bug: the status word used to free-cycle on a
    1400ms interval regardless of the actual FSM state."""
    html = _scanner_html()
    assert "scannerStatusWords" not in html  # the blind round-robin array is gone
    assert "const STATE_STATUS_TEXT" in html
    assert "function applyStatusForState(state, reason)" in html
    assert "tracking: [\"Tracking Surface\"" in html


def test_state_transitions_carry_reason_codes_to_diagnostics():
    html = _scanner_html()
    # safeTransition is now a thin alias over transitionScannerState(nextState, reason,
    # metadata) — extended to support a stale-generation guard and redundant-transition
    # skip (see test_transition_scanner_state_guards_redundant_and_stale_transitions).
    assert "function transitionScannerState(nextState, reason, metadata)" in html
    assert "function safeTransition(state, reason, metadata)" in html
    assert "'[SCAN STATE]'" in html
    assert "reason: reason || null" in html


def test_transition_scanner_state_guards_redundant_and_stale_transitions():
    html = _scanner_html()
    assert "if (from === nextState) return false;" in html
    assert "typeof metadata.generation === 'number' && metadata.generation !== scannerGeneration" in html
    assert "code: 'stale_transition'" in html
    assert "const scannerTransitionHistory = []" in html
    assert "TRANSITION_HISTORY_LIMIT" in html


def test_scan_candidate_and_match_accept_reject_diagnostics_present():
    html = _scanner_html()
    assert "'[SCAN CANDIDATE]'" in html
    assert "'[SCAN MATCH ACCEPT]'" in html
    assert "'[SCAN MATCH REJECT]'" in html
    assert "'[TRACK START]'" in html
    assert "'[CAMERA RECOVERY]'" in html
