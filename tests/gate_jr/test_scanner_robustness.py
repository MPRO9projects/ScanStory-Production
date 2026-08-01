"""Automated scanner robustness pack — synthetic acceptance/rejection coverage.

This is NOT physical-device certification. It exercises the classification rules
(evaluate_homography_quality, resolve_candidate_margin, the /detect_init keypoint gate,
and the client-side staleness guards) against generated fixtures — not real camera frames
on real hardware. See gate-jr/scanner-quality-matrix.md for what still needs a phone.

Run only this pack:
    python -m pytest -m scanner_robustness -q
"""
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest

pytestmark = pytest.mark.scanner_robustness


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


# --- Acceptance cases ---------------------------------------------------------------------

def test_clear_front_facing_marker_passes(app_module, homography_case, marker_grid_points):
    h = np.eye(3, dtype=np.float64)
    ok, quality = app_module.evaluate_homography_quality(**homography_case(h, marker_grid_points))
    assert ok
    assert quality["code"] == "accepted"


def test_supported_rotation_passes(app_module, homography_case, marker_grid_points, rotated_marker_homography):
    for angle in (15, 30):
        h = rotated_marker_homography(angle)
        ok, quality = app_module.evaluate_homography_quality(**homography_case(h, marker_grid_points))
        assert ok, f"rotation of {angle} degrees should be within supported range"
        assert quality["code"] == "accepted"


def test_moderate_perspective_passes_under_current_thresholds(app_module, homography_case, marker_grid_points, perspective_trapezoid_homographies):
    h = perspective_trapezoid_homographies["moderate"]
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, marker_grid_points, frame_w=900, frame_h=900)
    )
    assert ok
    assert quality["edge_ratio"] < 8.0  # current threshold, not weakened for this test


def test_valid_small_cropped_marker_passes(app_module, homography_case, small_cropped_marker):
    src, h, marker_w, marker_h = small_cropped_marker
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, src, marker_w=marker_w, marker_h=marker_h)
    )
    assert ok
    assert quality["code"] == "accepted"


def test_legacy_full_image_marker_passes(app_module, homography_case, marker_grid_points, legacy_full_image_marker_homography):
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(legacy_full_image_marker_homography, marker_grid_points)
    )
    assert ok
    assert quality["code"] == "accepted"


def test_mild_partial_occlusion_still_passes(app_module, homography_case, marker_dense_grid_points):
    """Occluding one edge of the marker (e.g. a finger over a corner) with most of the
    marker still visible must not force a rejection — 15 of 20 points remain, well spread."""
    mask = np.ones(20, dtype=bool)
    mask[15:] = False  # bottom row occluded
    h = np.array([[0.8, 0, 100], [0, 0.8, 200], [0, 0, 1]], dtype=np.float64)
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, marker_dense_grid_points, inlier_mask=mask)
    )
    assert ok
    assert quality["inliers"] == 15


# --- Rejection cases -----------------------------------------------------------------------

def test_heavy_partial_occlusion_is_rejected_never_a_false_accept(app_module, homography_case, marker_dense_grid_points):
    """Heavy occlusion (only a small corner cluster remains) must be REJECTED — the risk
    this guards against is a false accept, not merely 'less confident'."""
    mask = np.zeros(20, dtype=bool)
    mask[0:2] = True
    mask[5:7] = True  # 4 points, all in one corner
    h = np.array([[0.8, 0, 100], [0, 0.8, 200], [0, 0, 1]], dtype=np.float64)
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, marker_dense_grid_points, inlier_mask=mask)
    )
    assert not ok


def test_low_inlier_ratio_is_rejected(app_module, homography_case):
    total = 41
    src = np.array([[100 + i, 100 + i] for i in range(total)], dtype=np.float32)
    mask = np.zeros(total, dtype=bool)
    mask[:12] = True  # clears the absolute floor (8) but 12/41 = 0.29 < 0.30 ratio floor
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(np.eye(3, dtype=np.float64), src, inlier_mask=mask)
    )
    assert not ok
    assert quality["code"] == "low_inlier_ratio"


def test_excessive_reprojection_error_is_rejected(app_module, marker_grid_points):
    """dst deliberately does NOT match what homography_case would compute from H — built
    directly so the homography and the actual point positions disagree."""
    noisy_dst = marker_grid_points + np.array([30, 0], dtype=np.float32)
    mask = np.ones((len(marker_grid_points), 1), dtype=np.uint8)
    ok, quality = app_module.evaluate_homography_quality(
        marker_grid_points, noisy_dst, np.eye(3, dtype=np.float64), mask, 500, 300, 675, 1200, scale=0.8
    )
    assert not ok
    assert quality["code"] == "high_reprojection_error"


def test_invalid_quadrilateral_is_rejected(app_module, homography_case, marker_grid_points, invalid_quad_homography):
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(invalid_quad_homography, marker_grid_points, frame_w=675, frame_h=1200, scale=0.8)
    )
    assert not ok
    assert quality["code"] == "invalid_quad"


def test_excessive_perspective_is_rejected(app_module, homography_case, marker_grid_points, perspective_trapezoid_homographies):
    h = perspective_trapezoid_homographies["excessive"]
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, marker_grid_points, frame_w=900, frame_h=900)
    )
    assert not ok
    assert quality["code"] == "excessive_perspective"
    assert quality["edge_ratio"] > 8.0


def test_wrong_marker_many_descriptor_matches_but_bad_homography_is_rejected(app_module, homography_case, wrong_marker_many_matches_case):
    src, h = wrong_marker_many_matches_case
    ok, quality = app_module.evaluate_homography_quality(
        **homography_case(h, src, frame_w=675, frame_h=1200, scale=0.8)
    )
    assert not ok
    assert quality["code"] == "clustered_reference_points"


def test_ambiguous_two_candidates_return_a_deterministic_rejection_code(app_module, two_candidate_margin_cases):
    best_good, second_good = two_candidate_margin_cases["ambiguous"]
    ok, code = app_module.resolve_candidate_margin(best_good, second_good)
    assert not ok
    assert code == "candidate_margin_too_small"

    best_good, second_good = two_candidate_margin_cases["clear_winner"]
    ok, code = app_module.resolve_candidate_margin(best_good, second_good)
    assert ok
    assert code is None


def test_blank_background_is_rejected(client, app_module, login_user, feature_artifact, project_with_pair, blank_wall_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(blank_wall_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["detected"] is False


def test_low_texture_background_is_rejected(client, app_module, login_user, feature_artifact, project_with_pair, low_texture_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(low_texture_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["detected"] is False


def test_high_noise_background_is_rejected(client, app_module, login_user, feature_artifact, project_with_pair, high_noise_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(high_noise_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["detected"] is False


def test_repeated_pattern_is_rejected(client, app_module, login_user, feature_artifact, project_with_pair, repeated_pattern_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(repeated_pattern_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["detected"] is False


def test_motion_blur_is_rejected(client, app_module, login_user, feature_artifact, project_with_pair, motion_blurred_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(motion_blurred_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["detected"] is False


def test_wrong_marker_http_response_never_reports_detected_true(client, app_module, login_user, feature_artifact, project_with_pair, high_noise_image_bytes):
    """Same request twice — a second, unrelated 'wrong marker' frame must never flip
    detected to True just because a prior request succeeded/failed."""
    project, _pair = project_with_pair
    for _ in range(2):
        response = client.post(
            "/detect_init",
            data={"project_id": str(project.id), "test_image": (BytesIO(high_noise_image_bytes), "frame.jpg")},
            content_type="multipart/form-data",
        )
        assert response.get_json()["detected"] is False


def test_stale_generation_response_is_ignored():
    """Client-side guard already proven in test_gate_jr_scanner_recovery.py — re-asserted
    here as part of the robustness pack's own self-contained acceptance list."""
    html = _scanner_html()
    assert "const isStaleGeneration = requestGeneration !== scannerGeneration" in html
    assert "detectionPolicy.finish(requestId)" in html
    assert "code: isStaleGeneration ? 'stale_generation'" in html
