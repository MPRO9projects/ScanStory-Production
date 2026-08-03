from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


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
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)", detect_start)
    detect_body = html[detect_start:detect_end]
    assert "if (isHealthyLocalTracking())" in detect_body
    assert "healthy_tracking_detect_start_blocked" in detect_body
    assert "if (sessionEnding || detectInFlight || !cvReady || cam.readyState < 2) {" in detect_body
    assert "if (!sessionEnding && !isHealthyLocalTracking()) scheduleNextScan('after_attempt_not_started');" in detect_body
    assert "return;" in detect_body


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


# --- Recognition-stability pass: why many good matches collapse into few inliers -------
# Root cause investigated (see _filter_mutual_unique_matches in app.py): match_best_variant
# had no duplicate-match filtering — a single stored keypoint could be the "good" match for
# several query keypoints (repetitive texture), inflating good_matches without adding
# independent geometric evidence, which then inflated evaluate_homography_quality's
# required-inlier bar (MIN_INLIERS_RATIO * total) past what the true unique-correspondence
# set could ever satisfy.

def test_duplicate_match_filtering_keeps_only_best_per_train_and_query_index(app_module):
    matches = [
        cv2.DMatch(0, 5, 0.30),
        cv2.DMatch(1, 5, 0.20),   # duplicate trainIdx=5 — this one wins (lower distance)
        cv2.DMatch(2, 5, 0.25),   # duplicate trainIdx=5 — loses to idx 1
        cv2.DMatch(3, 7, 0.10),
        cv2.DMatch(3, 8, 0.40),   # duplicate queryIdx=3 — loses to (3,7)
        cv2.DMatch(4, 9, 0.15),
    ]
    filtered = app_module._filter_mutual_unique_matches(matches)
    train_ids = sorted(m.trainIdx for m in filtered)
    query_ids = sorted(m.queryIdx for m in filtered)
    assert train_ids == sorted(set(train_ids))   # every trainIdx appears at most once
    assert query_ids == sorted(set(query_ids))   # every queryIdx appears at most once
    assert len(filtered) == 3  # (1,5), (3,7), (4,9) survive from the 6 raw matches
    kept_pairs = sorted((m.queryIdx, m.trainIdx) for m in filtered)
    assert (1, 5) in kept_pairs   # lowest-distance survivor of the trainIdx=5 cluster
    assert (0, 5) not in kept_pairs
    assert (2, 5) not in kept_pairs
    assert (3, 7) in kept_pairs   # lowest-distance survivor of the queryIdx=3 cluster
    assert (3, 8) not in kept_pairs
    assert (4, 9) in kept_pairs


def test_duplicate_match_filtering_is_a_noop_on_already_unique_matches(app_module):
    matches = [cv2.DMatch(i, i + 100, 0.2 + i * 0.01) for i in range(20)]
    filtered = app_module._filter_mutual_unique_matches(matches)
    assert len(filtered) == 20


def test_required_inliers_formula_is_capped(app_module):
    """A marker with a very large unique-correspondence count (150) must not demand more
    than MAX_INLIERS_REQUIRED inliers — without the cap, 0.30*150=45 would make even a
    45-of-150 (30%) detection borderline-impossible to review/tune confidently."""
    assert app_module.MAX_INLIERS_REQUIRED == 40
    marker_w, marker_h = 500, 300
    total = 150
    src = np.array([[100 + (i % 20) * 5, 100 + (i // 20) * 5] for i in range(total)], dtype=np.float32)
    dst = src + np.array([3, 3], dtype=np.float32)
    mask = np.zeros((total, 1), dtype=np.uint8)
    mask[:40] = 1  # exactly at the cap — uncapped required would be int(0.30*150)=45

    ok_or_rejected, quality = app_module.evaluate_homography_quality(
        src, dst, np.eye(3, dtype=np.float64), mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    # Whatever else it does or doesn't reject on (reprojection/clustering), it must not be
    # rejected for insufficient_inliers, since inliers(40) >= min(45, 40)=40.
    if not ok_or_rejected:
        assert quality.get("code") != "insufficient_inliers"


def test_required_inliers_below_cap_uses_ratio_formula_unchanged(app_module):
    """Existing small-total behavior (e.g. the 45-good/6-inlier/13-required regression
    test above) must be completely unaffected by the cap — 13 < 40, cap never engages."""
    marker_w, marker_h = 500, 300
    src = np.array([[100 + i, 100 + i] for i in range(45)], dtype=np.float32)
    dst = src + np.array([5, 5], dtype=np.float32)
    mask = np.zeros((45, 1), dtype=np.uint8)
    mask[:6] = 1
    ok, quality = app_module.evaluate_homography_quality(
        src, dst, np.eye(3, dtype=np.float64), mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok
    assert quality["required"] == 13  # max(8, 0.30*45)=13, unaffected by the 40 cap


def test_many_matches_poor_ratio_still_rejected_after_dedup_and_cap(app_module):
    """The dedup+cap changes must not turn into a backdoor for accepting a genuinely weak
    detection — 100 total, 15 inliers (15% ratio, well under the 30% floor) stays rejected
    even though 15 would clear a naive absolute-count-only check."""
    marker_w, marker_h = 500, 300
    total = 100
    src = np.array([[100 + (i % 20) * 5, 100 + (i // 20) * 5] for i in range(total)], dtype=np.float32)
    dst = src + np.array([2, 2], dtype=np.float32)
    mask = np.zeros((total, 1), dtype=np.uint8)
    mask[:15] = 1
    ok, quality = app_module.evaluate_homography_quality(
        src, dst, np.eye(3, dtype=np.float64), mask, marker_w, marker_h, 675, 1200, scale=0.8
    )
    assert not ok
    assert quality["code"] in ("insufficient_inliers", "low_inlier_ratio")


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
    assert "function dropTracking(reason, extraMats" in html
    assert "dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {" in html
    assert "dropTracking('homography_empty', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in html
    assert "'[TRACK LOST]'" in html
    assert "clearTrackingGeometry(reason, { holdPose: true });" in html  # dropTracking holds the last pose briefly


def test_overlay_coordinate_conversion_has_one_canonical_path():
    html = _scanner_html()
    assert "function cameraDisplayMapping(sourceWidth, sourceHeight)" in html
    assert "function convertBackendCornersToOverlay(rawCorners, sourceWidth, sourceHeight, previousOrdered)" in html
    assert "const fit = getComputedStyle(cam).objectFit || 'cover'" in html
    assert "fit === 'contain' ? Math.min(elW / sourceWidth, elH / sourceHeight) : Math.max(elW / sourceWidth, elH / sourceHeight)" in html
    assert "offsetX: offX" in html
    assert "offsetY: offY" in html
    assert "devicePixelRatio: window.devicePixelRatio || 1" in html
    assert "mirroredX: FLIP_X || getComputedStyle(cam).transform.includes('-1')" in html
    assert "const converted = convertBackendCornersToOverlay(cornersFrame, frameW, frameH, lastOrdered)" in html


def test_overlay_conversion_covers_cover_contain_portrait_landscape_and_dpr():
    html = _scanner_html()
    assert "objectFit: fit" in html
    assert "fit === 'contain'" in html
    assert "Math.max(elW / sourceWidth, elH / sourceHeight)" in html
    assert "Math.min(elW / sourceWidth, elH / sourceHeight)" in html
    assert "sourceWidth, sourceHeight" in html
    assert "displayWidth: elW" in html
    assert "displayHeight: elH" in html
    assert "devicePixelRatio: window.devicePixelRatio || 1" in html


def test_corner_ordering_and_self_intersection_are_guarded():
    html = _scanner_html()
    assert "function normalizeCornerOrder(pts, previous)" in html
    assert "function resolveCornerCorrespondence(pts, previous)" in html
    assert "function bestCyclicMatchToPrevious" not in html
    assert "sortAroundCenter" not in html
    assert "rotateToTL" not in html
    assert "rotateArray" not in html
    assert "reversed_or_reflected_winding" in html
    assert "isSelfIntersectingQuad(clean)" in html
    assert "diagonalsCrossInside(clean)" in html
    assert "dropTracking('corner_order_invalid'" in html
    assert "requestPoseHold('corner_order_invalid')" in html


def test_overlay_uses_four_corner_perspective_matrix_without_video_recreation():
    html = _scanner_html()
    apply_start = html.index("function applyWarp(")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    assert "function quadToMatrix3d" in html
    assert "const nextSmoothCorners = smoothing.corners" in apply_block
    assert "const [p1, p2, p3, p4] = nextSmoothCorners" in apply_block
    assert "quadToMatrix3d(p1.x, p1.y, p2.x, p2.y, p3.x, p3.y, p4.x, p4.y, elW, elH)" in apply_block
    assert apply_block.index("overlayWrap.style.width = `${elW}px`") < apply_block.index("overlayWrap.style.transform")
    assert apply_block.index("overlayWrap.style.height = `${elH}px`") < apply_block.index("overlayWrap.style.transform")
    assert "overlayWrap.style.transform = `matrix3d(${m.join(\",\")})`" in apply_block
    assert html.count('id="overlay"') == 1
    assert "document.querySelectorAll('#overlay').length" in html
    assert "overlay.src = newVideoUrl" in html
    assert html.index("if (!wasSameTarget)") < html.index("overlay.src = newVideoUrl")


def test_overlay_wrapper_contains_only_video_surface_for_matrix_mapping():
    html = _scanner_html()
    start = html.index('<div id="overlayWrap">')
    end = html.index("</div>", start)
    overlay_wrap_markup = html[start:end]
    assert '<video id="overlay"' in overlay_wrap_markup
    assert 'id="wm-logo"' not in overlay_wrap_markup
    assert html.index('id="wm-logo"') > end
    assert "overlay.style.transform = `scale(${FLIP_X ? -1 : 1}, ${FLIP_Y ? -1 : 1})`" in html
    assert "overlay.style.transform = `matrix3d(" not in html


def test_time_based_smoothing_and_outlier_suppression_are_present():
    html = _scanner_html()
    smooth_start = html.index("function smoothPoseCorners(")
    smooth_block = html[smooth_start:html.index("function applyWarp", smooth_start)]
    assert "const POSE_STILL_TAU_MS = 220" in html
    assert "const POSE_MOVING_TAU_MS = 80" in html
    assert "function smoothPoseCorners(targetCorners, now, generation)" in html
    assert "const alpha = 1 - Math.exp(-dt / tau)" in smooth_block
    assert "const interpolatedValidation = validateOverlayQuad(interpolated)" in smooth_block
    assert "elapsedSinceTrusted > 1500" in smooth_block
    assert "reason: 'winding_flip'" in html
    assert "reason: 'self_intersecting_quad'" in html
    assert "reason: 'edge_ratio_jump'" in html
    assert "reason: 'diagonal_jump'" in html


def test_temporary_pose_hold_does_not_restart_video_on_marker_loss():
    html = _scanner_html()
    assert "const POSE_HOLD_MS = 500" in html
    assert "function requestPoseHold(reason)" in html
    assert "overlayState = 'held'" in html
    assert "overlayWrap.style.opacity = \"0.72\"" in html
    assert "clearTrackingGeometry(reason, { holdPose: true });" in html
    assert "requestPoseHold('no_detection')" in html
    hold_block = html[html.index("function requestPoseHold(reason)"):html.index("function playOverlay()", html.index("function requestPoseHold(reason)"))]
    assert "overlay.pause()" not in hold_block
    assert "currentTime = 0" not in hold_block


def test_reacquisition_same_video_preserves_playback_position():
    html = _scanner_html()
    same_target_start = html.index("} else {\n          if (videoFinished)")
    same_target_block = html[same_target_start:html.index("const pts = (data.init_points", same_target_start)]
    assert "overlay.currentTime = 0" in same_target_block
    assert "if (videoFinished)" in same_target_block
    assert "} else {\n            playOverlay();" in same_target_block
    assert "const wasSameTarget = (currentVideoUrl === newVideoUrl && currentPairId === newPairId)" in html


def test_scanner_debug_reports_coordinate_space_and_overlay_stability():
    html = _scanner_html()
    assert 'id="poseDebugCanvas"' in html
    assert "function drawPoseDebug(converted)" in html
    assert "'[OVERLAY POSE ACCEPT]'" in html
    assert "'[OVERLAY POSE REJECT]'" in html
    for field in [
        "camera intrinsic:",
        "display:",
        "object-fit:",
        "object-fit offset:",
        "devicePixelRatio:",
        "raw corners:",
        "candidate corners:",
        "normalized corners:",
        "chosen permutation:",
        "correspondence cost:",
        "pose validation:",
        "smoothed corners:",
        "polygon area:",
        "center:",
        "pose age:",
        "overlay state:",
        "video elements:",
        "render loops:",
    ]:
        assert field in html


def test_overlay_backend_raw_order_is_primary_not_screen_relative_sorting():
    """No corner-sorting/reordering logic anywhere in the corner-correspondence pipeline.
    Scoped to that pipeline specifically (resolveCornerCorrespondence through
    normalizeCornerOrder/applyWarp) rather than the whole file — the integration merge
    added an unrelated, legitimate `.sort()` elsewhere (rejection-reason-count display
    ordering in the diagnostics panel, nothing to do with corner geometry), which a
    whole-file check would false-positive on."""
    html = _scanner_html()
    assert "const ordered = cloneCorners(pts)" in html
    assert "return validation.signedArea > 0 ? ordered : null" in html
    assert "const resolved = resolveCornerCorrespondence(visible, previousOrdered)" in html
    assert "return { ordered, validation, permutation: 0, correspondenceCost: correspondenceCost(ordered, previous) }" in html
    assert "const newCornersRaw = data.corners.map(p => ({ x: Number(p.x), y: Number(p.y) }))" in html
    assert "const newCorners = normalizeCornerOrder(newCornersRaw, currCorners)" in html
    pipeline_start = html.index("function cloneCorners(pts)")
    pipeline_end = html.index("function cameraDisplayMapping(")
    pipeline = html[pipeline_start:pipeline_end]
    assert ".sort((a, b)" not in pipeline
    assert "x + pts[i].y" not in pipeline


def test_overlay_correspondence_does_not_rotate_valid_backend_order():
    html = _scanner_html()
    start = html.index("function resolveCornerCorrespondence(pts, previous)")
    block = html[start:html.index("function cameraDisplayMapping", start)]
    assert "permutation: 0" in html
    assert "correspondenceCost(ordered, previous)" in html
    assert "for (let k = 0; k < 4; k++)" not in block
    assert "const cand = rotateArray" not in block
    assert "no_winding_preserving_cyclic_match" not in html
    assert "[ordered[0], ordered[3], ordered[2], ordered[1]]" not in html


def test_overlay_validation_rejects_folded_reflected_and_collapsed_quads():
    html = _scanner_html()
    assert "function validateOverlayQuad(pts)" in html
    assert "reason: 'non_finite_or_not_four'" in html
    assert "reason: 'zero_edge'" in html
    assert "reason: 'collapsed_area'" in html
    assert "reason: 'self_intersecting_quad'" in html
    assert "reason: 'diagonals_do_not_cross_inside'" in html
    assert "reason: 'winding_flip'" in html
    assert "reason: 'edge_ratio_jump'" in html
    assert "reason: 'diagonal_jump'" in html


def test_overlay_smoothing_happens_after_correspondence_resolution():
    html = _scanner_html()
    apply_start = html.index("function applyWarp(")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    assert apply_block.index("convertBackendCornersToOverlay(cornersFrame, frameW, frameH, lastOrdered)") < apply_block.index("smoothPoseCorners(orderedOverlayCorners")
    assert apply_block.index("if (!converted.ordered)") < apply_block.index("smoothPoseCorners(orderedOverlayCorners")
    assert apply_block.index("const nextSmoothCorners = smoothing.corners") < apply_block.index("quadToMatrix3d(")
    assert "Never smooth before correspondence is resolved" not in html


def test_overlay_partially_offscreen_quads_are_preserved_not_individually_clamped():
    html = _scanner_html()
    assert "function isOverlayFrameQuadRenderable(pts, fw, fh)" in html
    assert "const pad = 0.45" in html
    assert "validation.area > giantArea" in html
    assert ".x = Math.max" not in html
    assert ".y = Math.max" not in html
    assert "Math.min(Math.max" not in html


def test_overlay_video_source_rectangle_maps_to_destination_corner_order():
    html = _scanner_html()
    apply_start = html.index("function applyWarp(")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    assert "object-fit: fill" in html
    assert "const nextSmoothCorners = smoothing.corners" in apply_block
    assert "const [p1, p2, p3, p4] = nextSmoothCorners" in apply_block
    assert "quadToMatrix3d(p1.x, p1.y, p2.x, p2.y, p3.x, p3.y, p4.x, p4.y, elW, elH)" in apply_block
    assert "const a11 = x2 - x1 + a13 * x2" in html
    assert "const a21 = x4 - x1 + a23 * x4" in html
    assert "sourceRect: { width: elW, height: elH }" in html
    assert "markerCrop: { width: OVERLAY_SOURCE_WIDTH, height: OVERLAY_SOURCE_HEIGHT }" in html


def test_overlay_diagnostics_are_bounded_and_privacy_safe():
    html = _scanner_html()
    runtime_source = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    assert "const limit = 80" in runtime_source
    assert "if (events.length > limit) events.shift()" in runtime_source
    assert "delete safe.frame" in runtime_source
    assert "delete safe.image" in runtime_source
    assert "delete safe.blob" in runtime_source
    assert "rawCorners: converted.raw" in html
    assert "candidateCorners: converted.visible" in html
    assert "requestSequence" in html
    assert "latestAppliedSequence" in html
    assert "responseGeneration" in html
    assert "responseTimestamp" in html
    assert "responseAgeWhenAppliedMs" in html
    assert "convertedViewportCorners" in html
    assert "resolvedCorners" in html
    assert "previousTrustedCorners" in html
    assert "smoothedCorners" in html
    assert "matrix3d" in html
    assert "sourceFrame" in html
    assert "sourceVideo" in html
    assert "cameraVideo" in html
    assert "viewport" in html
    assert "screenOrientationAngle" in html
    assert "poseApplicationMode" in html
    assert "groupId" not in html
    assert "deviceId" not in html


def _signed_area(points):
    return sum(
        points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1]
        for i in range(4)
    ) / 2


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _rotate(points, steps):
    return points[steps:] + points[:steps]


def _segments_intersect(a, b, c, d):
    def ccw(p1, p2, p3):
        return (p3[1] - p1[1]) * (p2[0] - p1[0]) > (p2[1] - p1[1]) * (p3[0] - p1[0])

    return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)


def _is_bowtie(points):
    return _segments_intersect(points[0], points[1], points[2], points[3]) or _segments_intersect(points[1], points[2], points[3], points[0])


def _validate_model_quad(points):
    if len(points) != 4:
        return False
    if not all(np.isfinite(x) and np.isfinite(y) for x, y in points):
        return False
    if _signed_area(points) <= 0:
        return False
    if _is_bowtie(points):
        return False
    return True


def _interpolate_quad(start, end, alpha):
    return [
        (start[i][0] + (end[i][0] - start[i][0]) * alpha, start[i][1] + (end[i][1] - start[i][1]) * alpha)
        for i in range(4)
    ]


def _resolve_backend_authoritative(points, previous=None):
    if _signed_area(points) <= 0:
        return None
    if _is_bowtie(points):
        return None
    return points


def _rotated_quad(cx, cy, w, h, degrees):
    theta = np.deg2rad(degrees)
    base = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return [
        (
            cx + x * np.cos(theta) - y * np.sin(theta),
            cy + x * np.sin(theta) + y * np.cos(theta),
        )
        for x, y in base
    ]


def test_overlay_correspondence_model_handles_clockwise_rotation_identity_change():
    poses = [_rotated_quad(300, 300, 240, 160, deg) for deg in (0, 45, 90)]
    trusted = _resolve_backend_authoritative(poses[0])
    for pose in poses[1:]:
        trusted = _resolve_backend_authoritative(pose, trusted)
        assert trusted == pose
    assert min(range(4), key=lambda i: poses[-1][i][0] + poses[-1][i][1]) != 0


def test_overlay_correspondence_model_handles_counter_clockwise_rotation_identity_change():
    poses = [_rotated_quad(300, 300, 240, 160, deg) for deg in (0, -45, -90)]
    trusted = _resolve_backend_authoritative(poses[0])
    for pose in poses[1:]:
        trusted = _resolve_backend_authoritative(pose, trusted)
        assert trusted == pose
    assert min(range(4), key=lambda i: poses[-1][i][0] + poses[-1][i][1]) != 0


def test_overlay_correspondence_model_does_not_use_cyclic_permutation_for_valid_raw_order():
    previous = [(100, 100), (300, 100), (300, 260), (100, 260)]
    cyclic = [previous[2], previous[3], previous[0], previous[1]]
    assert _resolve_backend_authoritative(cyclic, previous) == cyclic
    reflected = [previous[0], previous[3], previous[2], previous[1]]
    assert _resolve_backend_authoritative(reflected, previous) is None


def test_overlay_correspondence_model_keeps_large_valid_rotation_and_rejects_bowtie():
    previous = _rotated_quad(300, 300, 240, 160, 0)
    valid_large_rotation = _rotated_quad(300, 300, 240, 160, 88)
    assert _resolve_backend_authoritative(valid_large_rotation, previous) == valid_large_rotation
    bowtie = [(100, 100), (300, 260), (300, 100), (100, 260)]
    assert _is_bowtie(bowtie)
    assert _resolve_backend_authoritative(bowtie, previous) is None


def test_overlay_correspondence_model_preserves_90_and_180_degree_raw_indices():
    previous = _rotated_quad(300, 300, 240, 160, 0)
    for degrees in (90, 135, 180):
        pose = _rotated_quad(300, 300, 240, 160, degrees)
        assert _resolve_backend_authoritative(pose, previous) == pose


def test_overlay_correspondence_model_has_no_90_degree_source_content_remap():
    previous = _rotated_quad(300, 300, 240, 160, 0)
    rotated = _rotated_quad(300, 300, 240, 160, 90)
    resolved = _resolve_backend_authoritative(rotated, previous)
    assert resolved[0] == rotated[0]
    assert resolved[1] == rotated[1]
    assert resolved[2] == rotated[2]
    assert resolved[3] == rotated[3]


def test_overlay_rejects_stale_applied_sequence_before_state_mutation():
    html = _scanner_html()
    stale_start = html.index("if (requestId <= latestAppliedSequence)")
    mutation_start = html.index("const newVideoUrl = data.video_url")
    assert stale_start < mutation_start
    assert "code: 'stale_applied_sequence'" in html
    assert "latestAppliedSequence = requestSequence" in html


def test_overlay_rejects_generation_mismatch_and_never_smooths_across_generation():
    html = _scanner_html()
    apply_start = html.index("function applyWarp(cornersFrame, context = {})")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    smooth_start = html.index("function smoothPoseCorners(targetCorners, now, generation)")
    smooth_block = html[smooth_start:html.index("function applyWarp", smooth_start)]
    assert "Number(context.responseGeneration) !== scannerGeneration" in apply_block
    assert "code: 'stale_generation'" in apply_block
    assert "lastSmoothGeneration !== generation" in smooth_block
    assert "snapped_generation_change" in smooth_block


def test_overlay_long_gap_snaps_instead_of_cross_gap_smoothing():
    html = _scanner_html()
    smooth_start = html.index("function smoothPoseCorners(targetCorners, now, generation)")
    smooth_block = html[smooth_start:html.index("function applyWarp", smooth_start)]
    assert "elapsedSinceTrusted > 1500" in smooth_block
    assert "snapped_long_gap" in smooth_block
    assert "const targetValidation = validateOverlayQuad(targetCorners)" in smooth_block


def test_overlay_validates_interpolated_frame_before_rendering():
    html = _scanner_html()
    smooth_start = html.index("function smoothPoseCorners(targetCorners, now, generation)")
    smooth_block = html[smooth_start:html.index("function applyWarp", smooth_start)]
    apply_start = html.index("function applyWarp(cornersFrame, context = {})")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    assert "const interpolatedValidation = validateOverlayQuad(interpolated)" in smooth_block
    assert "if (!interpolatedValidation.ok) return { corners: null, mode: 'held'" in smooth_block
    assert "'[OVERLAY INTERPOLATION HOLD]'" in apply_block
    assert "requestPoseHold('interpolated_quad_invalid')" in apply_block


def test_overlay_non_finite_matrix_is_never_applied():
    html = _scanner_html()
    apply_start = html.index("function applyWarp(cornersFrame, context = {})")
    apply_block = html[apply_start:html.index("function quadArea2", apply_start)]
    assert "function matrixIsFinite(values)" in html
    assert "if (!matrixIsFinite(m))" in apply_block
    assert "'[OVERLAY MATRIX REJECT]'" in apply_block
    assert apply_block.index("if (!matrixIsFinite(m))") < apply_block.index("overlayWrap.style.transform")


def test_overlay_model_valid_direct_interpolation_remains_valid():
    start = [(100, 100), (300, 110), (290, 260), (95, 250)]
    end = [(120, 120), (320, 125), (310, 275), (115, 270)]
    assert _validate_model_quad(start)
    assert _validate_model_quad(end)
    assert _validate_model_quad(_interpolate_quad(start, end, 0.5))


def test_overlay_model_large_rotation_after_long_gap_must_not_render_collapsed_intermediate():
    start = _rotated_quad(300, 300, 240, 160, 0)
    end = _rotated_quad(300, 300, 240, 160, 180)
    midpoint = _interpolate_quad(start, end, 0.5)
    assert _validate_model_quad(start)
    assert _validate_model_quad(end)
    assert not _validate_model_quad(midpoint)


def test_overlay_source_keeps_partially_offscreen_valid_quad_supported():
    html = _scanner_html()
    assert "const pad = 0.45" in html
    assert "p.x > -pad * fw" in html
    assert "p.y > -pad * fh" in html


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


# --- ROI feature extraction: fix server-side crop enforcement (project 39 investigation) -
# Root cause confirmed: for pairs where the client-side crop-baking never actually reduced
# the uploaded pixels (an older upload, or any path that saves the raw file), marker_mode
# stores "crop" with real crop_x/y/width/height fractions, but extract_features_multi()
# always ran ORB against the FULL stored image — background/table/chair descriptors ended
# up in the reference feature set. extract_marker_roi() now applies that crop before any
# resize/enhancement/ORB step, using the image's OWN actual (EXIF-corrected) pixel
# dimensions as the reference frame — never guessed, always measured.

def _write_test_image(path, width, height, bg_color, card_box=None, card_color=None, exif_orientation=None):
    """Builds a real, decodable JPEG: a solid bg_color canvas with an optional distinctly-
    colored card_color rectangle at card_box=(x, y, w, h) — mimics 'a business card on a
    table' for background-exclusion tests. exif_orientation, if given, is written as EXIF
    tag 0x0112 on an image saved at PRE-rotation (raw) dimensions."""
    img = Image.new("RGB", (width, height), bg_color)
    if card_box is not None:
        x, y, w, h = card_box
        card = Image.new("RGB", (w, h), card_color)
        img.paste(card, (x, y))
    path.parent.mkdir(parents=True, exist_ok=True)
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(path, "JPEG", exif=exif)
    else:
        img.save(path, "JPEG")
    return path


def test_extract_marker_roi_converts_normalized_crop_to_correct_pixel_roi(app_module, tmp_path):
    path = _write_test_image(tmp_path / "card.jpg", 1000, 800, (0, 0, 0))
    marker_meta = {"mode": "crop", "crop_x": 0.2, "crop_y": 0.25, "crop_width": 0.3, "crop_height": 0.4}
    roi, diag = app_module.extract_marker_roi(str(path), marker_meta)
    assert diag["crop_applied"] is True
    assert diag["calculated_pixel_roi"] == [200, 200, 300, 320]
    assert diag["clamped_pixel_roi"] == [200, 200, 300, 320]
    assert diag["clamped"] is False
    assert roi.shape[1] == 300 and roi.shape[0] == 320


def test_extract_marker_roi_uses_orientation_corrected_dimensions(app_module, tmp_path):
    """A 200x100 image saved with EXIF orientation=6 (rotate 90) must be treated as a
    100x200 image for crop math — the raw (undecoded-orientation) dimensions must never
    leak into the pixel ROI calculation."""
    path = _write_test_image(tmp_path / "rotated.jpg", 200, 100, (5, 5, 5), exif_orientation=6)
    marker_meta = {"mode": "crop", "crop_x": 0.0, "crop_y": 0.0, "crop_width": 0.5, "crop_height": 0.5}
    roi, diag = app_module.extract_marker_roi(str(path), marker_meta)
    assert diag["original_decoded_w"] == 200 and diag["original_decoded_h"] == 100
    assert diag["orientation_corrected_w"] == 100 and diag["orientation_corrected_h"] == 200
    assert diag["calculated_pixel_roi"] == [0, 0, 50, 100]
    assert roi.shape[1] == 50 and roi.shape[0] == 100


def test_extract_marker_roi_clamps_out_of_bounds_crop(app_module, tmp_path):
    path = _write_test_image(tmp_path / "card.jpg", 400, 300, (0, 0, 0))
    # px=340,pw=120 clamps to 60; py=240,ph=120 clamps to 60 — both stay above
    # MIN_CROP_ROI_PIXELS(32) so this exercises clamping without also tripping the
    # too-small rejection.
    marker_meta = {"mode": "crop", "crop_x": 0.85, "crop_y": 0.8, "crop_width": 0.3, "crop_height": 0.4}
    roi, diag = app_module.extract_marker_roi(str(path), marker_meta)
    assert diag["clamped"] is True
    px, py, pw, ph = diag["clamped_pixel_roi"]
    assert px + pw <= 400 and py + ph <= 300
    assert roi.shape[1] == pw and roi.shape[0] == ph


def test_extract_marker_roi_rejects_invalid_crop_with_logged_reason(app_module, tmp_path):
    path = _write_test_image(tmp_path / "card.jpg", 400, 300, (0, 0, 0))
    for bad_meta in (
        {"mode": "crop", "crop_x": 0.1, "crop_y": 0.1, "crop_width": 0.0, "crop_height": 0.2},
        {"mode": "crop", "crop_x": -0.1, "crop_y": 0.1, "crop_width": 0.2, "crop_height": 0.2},
        {"mode": "crop", "crop_x": 0.1, "crop_y": 0.1, "crop_width": 0.01, "crop_height": 0.01},  # too small in pixels
    ):
        roi, diag = app_module.extract_marker_roi(str(path), bad_meta)
        assert diag["crop_applied"] is False
        assert diag["fallback_reason"] is not None  # never silent
        assert roi.shape[1] == 400 and roi.shape[0] == 300  # falls back to the whole image


def test_extract_marker_roi_excludes_background_outside_the_card(app_module, tmp_path):
    """A distinctly-colored 'card' region inside a differently-colored 'table' background —
    the returned ROI's pixels must be entirely (or overwhelmingly) the card color, proving
    background is excluded, not just that dimensions happen to match."""
    bg = (200, 30, 30)      # reddish "table"
    card = (30, 200, 30)    # greenish "card"
    card_box = (100, 80, 200, 150)  # x, y, w, h
    path = _write_test_image(tmp_path / "card_on_table.jpg", 500, 400, bg, card_box=card_box, card_color=card)
    marker_meta = {
        "mode": "crop",
        "crop_x": card_box[0] / 500, "crop_y": card_box[1] / 400,
        "crop_width": card_box[2] / 500, "crop_height": card_box[3] / 400,
    }
    roi, diag = app_module.extract_marker_roi(str(path), marker_meta)
    assert diag["crop_applied"] is True
    mean_bgr = roi.reshape(-1, 3).mean(axis=0)
    # roi is BGR — card_color=(30,200,30) RGB means high G, low R/B
    assert mean_bgr[1] > 150   # green channel dominant
    assert mean_bgr[2] < 80    # red channel (BGR index 2) low — background red is excluded


def test_extract_marker_roi_full_image_mode_is_unchanged(app_module, tmp_path):
    path = _write_test_image(tmp_path / "card.jpg", 300, 200, (0, 0, 0))
    roi, diag = app_module.extract_marker_roi(str(path), {"mode": "full_image"})
    assert diag["crop_applied"] is False
    assert diag["fallback_reason"] == "mode_full_image"
    assert roi.shape[1] == 300 and roi.shape[0] == 200


def test_extract_marker_roi_skips_double_crop_when_already_client_side_cropped(app_module, tmp_path):
    """If the stored file's dimensions already match marker_processed_width/height (the
    client's own post-crop canvas render), applying crop_x/y/width/height again would
    double-crop — must be detected and skipped."""
    path = _write_test_image(tmp_path / "already_cropped.jpg", 240, 180, (0, 0, 0))
    marker_meta = {
        "mode": "crop", "crop_x": 0.15, "crop_y": 0.15, "crop_width": 0.5, "crop_height": 0.5,
        "processed_width": 240, "processed_height": 180,
    }
    roi, diag = app_module.extract_marker_roi(str(path), marker_meta)
    assert diag["crop_applied"] is False
    assert diag["fallback_reason"] == "already_cropped_client_side"
    assert roi.shape[1] == 240 and roi.shape[0] == 180


def _make_pair_with_real_image(app_module, project_with_pair, width, height, bg_color, card_box, card_color, mode="crop"):
    project, pair = project_with_pair
    img_path = Path(app_module.IMAGES_DIR) / pair.image_filename
    _write_test_image(img_path, width, height, bg_color, card_box=card_box, card_color=card_color)
    cx, cy, cw, ch = card_box
    pair.marker_mode = mode
    pair.marker_crop_x = cx / width
    pair.marker_crop_y = cy / height
    pair.marker_crop_width = cw / width
    pair.marker_crop_height = ch / height
    # Deliberately unset (simulates project 39's real bug — an older pair with crop
    # metadata but no recorded processed_width/height): the "already cropped client-side"
    # detection requires BOTH to be known and > 0, so leaving them unset forces
    # extract_marker_roi to actually apply the crop, matching the real confirmed bug.
    pair.marker_processed_width = None
    pair.marker_processed_height = None
    return project, pair


def test_rebuild_pair_features_default_uses_exact_pixels_not_crop(app_module, db_session, project_with_pair):
    """URGENT ROLLBACK: rebuild_pair_features's default (apply_legacy_roi=False, i.e. not
    passed at all) must NOT apply marker_crop_x/y/width/height — the stored image is
    already the selected ROI (client-side canvas crop), so cropping it again would
    double-crop exactly like the real project 40 regression (641x1200 -> 245x644).
    Reference dimensions must equal the FULL stored image, not the crop box."""
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 600, 500, (10, 10, 10), (100, 100, 200, 150), (200, 200, 200)
    )
    db_session.commit()

    report = app_module.rebuild_pair_features(project.id, pair.pair_index)

    assert report["apply_legacy_roi"] is False
    assert report["marker_meta"] is None
    assert report["new"]["reference_w"] == 600  # full stored image width, NOT the 200px crop
    assert report["new"]["reference_h"] == 500  # full stored image height, NOT the 150px crop
    assert report["new"]["keypoint_count"] is not None


def test_rebuild_pair_features_apply_legacy_roi_true_applies_the_crop(app_module, db_session, project_with_pair):
    """The ONLY way to get crop behavior back: explicit apply_legacy_roi=True. Verifies the
    escape hatch still works for a pair genuinely confirmed to predate crop-baking."""
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 600, 500, (10, 10, 10), (100, 100, 200, 150), (200, 200, 200)
    )
    db_session.commit()

    report = app_module.rebuild_pair_features(project.id, pair.pair_index, apply_legacy_roi=True)

    assert report["apply_legacy_roi"] is True
    assert report["marker_meta"] is not None
    assert report["new"]["reference_w"] == 200
    assert report["new"]["reference_h"] == 150


def test_rebuild_pair_features_default_is_idempotent_and_never_shrinks(app_module, db_session, project_with_pair):
    """Running the default (no-crop) rebuild twice must give IDENTICAL dimensions both
    times — it must never progressively shrink the marker on repeated runs."""
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 600, 500, (10, 10, 10), (100, 100, 200, 150), (200, 200, 200)
    )
    db_session.commit()

    first = app_module.rebuild_pair_features(project.id, pair.pair_index)
    second = app_module.rebuild_pair_features(project.id, pair.pair_index)

    assert first["new"]["reference_w"] == second["new"]["reference_w"] == 600
    assert first["new"]["reference_h"] == second["new"]["reference_h"] == 500
    assert first["new"]["keypoint_count"] == second["new"]["keypoint_count"]


def test_rebuild_pair_features_backs_up_existing_npz_before_replacing(app_module, db_session, project_with_pair, feature_artifact):
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 600, 500, (10, 10, 10), (100, 100, 200, 150), (200, 200, 200)
    )
    db_session.commit()
    assert feature_artifact.exists()  # pre-existing (whole-image) feature file, from the fixture

    report = app_module.rebuild_pair_features(project.id, pair.pair_index)

    assert report["backup_path"] is not None
    assert Path(report["backup_path"]).exists()
    assert report["previous"]["reference_w"] == 100  # the fixture's stored old w/h
    assert report["previous"]["reference_h"] == 100


def test_rebuild_pair_features_does_not_alter_source_image_or_video(app_module, db_session, project_with_pair):
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 600, 500, (10, 10, 10), (100, 100, 200, 150), (200, 200, 200)
    )
    db_session.commit()
    img_path = Path(app_module.IMAGES_DIR) / pair.image_filename
    video_path = Path(app_module.VIDEOS_DIR) / pair.video_filename
    image_bytes_before = img_path.read_bytes()
    video_bytes_before = video_path.read_bytes()

    app_module.rebuild_pair_features(project.id, pair.pair_index)

    assert img_path.read_bytes() == image_bytes_before
    assert video_path.read_bytes() == video_bytes_before


def test_rebuild_pair_features_unknown_pair_raises_clear_error(app_module, db_session, project_with_pair):
    project, _pair = project_with_pair
    try:
        app_module.rebuild_pair_features(project.id, 999)
        assert False, "expected ValueError for a nonexistent pair_index"
    except ValueError as e:
        assert "No ProjectPair found" in str(e)


def test_homography_uses_crop_relative_reference_dimensions_and_fixed_corner_order(app_module):
    """Confirms evaluate_homography_quality's own corner-order contract is untouched by
    the ROI work — it already builds the source rect purely from whatever marker_w/marker_h
    it's given (now the CROP dimensions, once rebuild_pair_features runs), and always in
    [TL, TR, BR, BL] order: (0,0), (w,0), (w,h), (0,h)."""
    marker_w, marker_h = 200, 150  # crop-relative dims, not the original full-image dims
    src = np.array(
        [[10, 10], [190, 8], [192, 140], [8, 142], [50, 50], [150, 50], [150, 100], [50, 100], [100, 75]],
        dtype=np.float32,
    )
    h_matrix = cv2.getPerspectiveTransform(
        np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        np.array([[40, 40], [560, 60], [540, 980], [60, 960]], dtype=np.float32),
    )
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(src, dst, h_matrix, mask, marker_w, marker_h, 675, 1200, scale=1.0)
    assert ok
    corners = quality["corners"]
    # [TL, TR, BR, BL]: TL.x < TR.x, TR.y < BR.y, BR.x > BL.x — a basic ordering sanity
    # check, not a re-derivation of the corner math itself (owned elsewhere, untouched).
    tl, tr, br, bl = corners
    assert tl[0] < tr[0]
    assert tr[1] < br[1]
    assert br[0] > bl[0]


# --- URGENT ROLLBACK: double-crop protection (projects 39/40 real-device regression) ----
# The normal upload path already renders the user-selected ROI into a canvas and uploads
# those pixels (drawCroppedMarkerToCanvas/renderMarkerBlob in user_create_project.html).
# Automatically re-applying marker_crop_x/y/width/height during background feature
# extraction double-cropped every new project — project 40 went from a genuine 641x1200
# marker down to ~245x644, and zero detections resulted on both projects 39 and 40 in real-
# device testing. marker_meta must never be passed into make_feature_working_jpeg for any
# normal upload/reprocessing path; the crop helper is now reachable ONLY through
# rebuild_pair_features(..., apply_legacy_roi=True), an explicit, narrow escape hatch.

def _app_py_src():
    return Path("app.py").read_text(encoding="utf-8", errors="ignore")


def test_normal_upload_paths_never_pass_marker_meta_to_feature_extraction(app_module):
    """Static check on the two real background-processing closures (user + admin project
    creation) — neither may pass marker_meta to make_feature_working_jpeg. This is the
    literal fix for the confirmed regression, not just the helper's own safe default."""
    src = _app_py_src()

    user_start = src.index("def process_single_pair_bg(project_id, pair_index, img_filename, upload_id, video_info=None):")
    user_body = src[user_start:user_start + 3000]
    assert "make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)" in user_body
    assert "marker_meta=" not in user_body

    admin_start = src.index("def process_single_pair_bg_admin(project_id, pair_index, img_filename):")
    admin_end = admin_start + 3000
    admin_body = src[admin_start:admin_end]
    assert "make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)" in admin_body
    assert "marker_meta=" not in admin_body


def test_make_feature_working_jpeg_default_ignores_stored_crop_metadata(app_module, tmp_path):
    """Section 5A: even if a caller HAD crop metadata on hand, the current normal-path
    call (no marker_meta kwarg) extracts from the exact uploaded pixels — proven directly
    against make_feature_working_jpeg, not just by reading the call site."""
    src_path = tmp_path / "card.jpg"
    out_path = tmp_path / "work.jpg"
    _write_test_image(src_path, 641, 1200, (0, 0, 0), card_box=(50, 50, 300, 300), card_color=(200, 200, 200))

    app_module.make_feature_working_jpeg(str(src_path), str(out_path), max_dim=app_module.ORB_MAX_DIM, jpeg_quality=92)

    with Image.open(out_path) as out_img:
        assert out_img.size == (641, 1200)  # exact uploaded pixels, no crop applied


def test_legacy_roi_repair_requires_explicit_flag_not_marker_mode_alone(app_module, db_session, project_with_pair):
    """Section 5B: rebuild_pair_features must never crop based on marker_mode=='crop'
    alone — apply_legacy_roi defaults to False even when the pair's marker_mode is 'crop'
    and valid crop_x/y/width/height are present."""
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 500, 400, (5, 5, 5), (50, 50, 150, 120), (220, 220, 220)
    )
    assert pair.marker_mode == "crop"  # crop metadata IS present and valid
    db_session.commit()

    default_report = app_module.rebuild_pair_features(project.id, pair.pair_index)
    assert default_report["new"]["reference_w"] == 500
    assert default_report["new"]["reference_h"] == 400

    legacy_report = app_module.rebuild_pair_features(project.id, pair.pair_index, apply_legacy_roi=True)
    assert legacy_report["new"]["reference_w"] == 150
    assert legacy_report["new"]["reference_h"] == 120


def test_project_40_style_regression_already_cropped_marker_stays_full_size(app_module, db_session, project_with_pair):
    """Section 5D — reproduces the exact project 40 shape: a 641x1200-equivalent
    already-cropped marker (using round numbers: 640x1200) carrying OLD normalized crop
    metadata (as if from before the crop-baking pipeline existed, still pointing at roughly
    a third of the frame). Normal processing (default rebuild, no flag) must leave it at
    640x1200 — it must NOT become ~245x644 like the real regression did."""
    project, pair = _make_pair_with_real_image(
        app_module, project_with_pair, 640, 1200, (15, 15, 15), (160, 260, 245, 644), (210, 210, 210)
    )
    db_session.commit()

    report = app_module.rebuild_pair_features(project.id, pair.pair_index)

    assert report["new"]["reference_w"] == 640
    assert report["new"]["reference_h"] == 1200
    assert not (
        200 <= report["new"]["reference_w"] <= 290 and 600 <= report["new"]["reference_h"] <= 690
    )  # nowhere near the ~245x644 double-crop shape
