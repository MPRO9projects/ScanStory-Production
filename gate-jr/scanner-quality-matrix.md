# Scanner Quality Matrix

Status of automated scanner-robustness coverage as of this pass. This is **not** physical-device
certification — it documents what the synthetic/automated test pack (`tests/gate_jr/test_scanner_robustness.py`,
run via `python -m pytest -m scanner_robustness -q`) actually proves, versus what still needs a real
phone in hand.

**How to read "Automated coverage":**
- **Rule-level (synthetic)** — exercises `evaluate_homography_quality`/`resolve_candidate_margin` directly
  against generated point clouds/homographies. Proves the classification *rule* is correct; says nothing
  about real ORB descriptors, real lighting, or real camera noise.
- **Browser-simulated (HTTP)** — a generated image (noise/gradient/checkerboard) posted to the real
  `/detect_init` endpoint through Flask's test client. Proves the endpoint's gates fire correctly on
  real OpenCV/ORB output for that image; still not a real camera or a real printed marker.
- **None** — no automated coverage; manual phone test is the only signal.

| Scenario | Expected result | Automated coverage | Manual phone test required | Current limitation | Diagnostic fields to inspect |
|---|---|---|---|---|---|
| Clear front-facing marker | Accept | Rule-level (`test_clear_front_facing_marker_passes`) | Yes | No real ORB descriptors/lighting | inliers, inlier ratio, quad valid |
| 90° rotated marker | Depends — not asserted as accept or reject; likely device/mode-dependent | None (only 15°/30° tested, see below) | Yes | 90° in-plane rotation not covered by this pack | tracking state, rejection reason |
| Smaller supported rotation (15°, 30°) | Accept | Rule-level (`test_supported_rotation_passes`) | Yes | Real optical-flow re-lock after rotation not exercised | inlier ratio, reprojection error |
| Moderate perspective skew | Accept (current thresholds) | Rule-level (`test_moderate_perspective_passes_under_current_thresholds`) | Yes | Hand-picked trapezoid, not a real lens/tilt sample | edge_ratio, quad valid |
| Excessive perspective skew | Reject (`excessive_perspective`) | Rule-level (`test_excessive_perspective_is_rejected`) | Yes | Same as above | edge_ratio, rejection reason |
| Partial occlusion (mild — one edge covered) | Accept | Rule-level (`test_mild_partial_occlusion_still_passes`) | Yes | Synthetic point removal, not a real finger/object over a lens | inliers, reference grid cells |
| Partial occlusion (heavy — most of marker covered) | Reject, never a false accept | Rule-level (`test_heavy_partial_occlusion_is_rejected_never_a_false_accept`) | Yes | Same as above | inliers, rejection reason |
| Small marker in large frame | Accept, not penalized for small screen footprint | Rule-level (`test_valid_small_cropped_marker_passes`; see also `test_marker_selection_upload.py::test_roi_coverage_is_separate_from_full_frame_position`) | Yes | No real crop-selection UI exercised here | projected ROI grid cells, frame grid cells |
| Motion blur | Reject (too few keypoints) | Browser-simulated (`test_motion_blur_is_rejected`) | Yes | Synthetic blur kernel over noise, not real motion blur physics | keypoints |
| Low contrast / low texture | Reject (too few keypoints) | Browser-simulated (`test_low_texture_background_is_rejected`) | Yes | No real low-light camera sensor noise | keypoints |
| High noise background | Reject (no match) | Browser-simulated (`test_high_noise_background_is_rejected`) | Yes | Random noise, not a real cluttered scene | keypoints, good matches |
| Repeated texture / pattern | Reject (no match to a stored marker) | Browser-simulated (`test_repeated_pattern_is_rejected`) | Yes | Checkerboard proxy; no real repeated-pattern registered marker to test *against* | good matches, rejection reason |
| Blank background / wall | Reject (too few keypoints) | Browser-simulated (`test_blank_background_is_rejected`) | Yes | Flat synthetic grey, not a real textured wall | keypoints |
| Wrong marker (unrelated content) | Reject | Browser-simulated (`test_wrong_marker_http_response_never_reports_detected_true`) | Yes | No second real registered marker exists to truly test "wrong marker matches someone else's" | good matches, matched_pair_id |
| Descriptor-rich but geometrically invalid candidate | Reject (`clustered_reference_points`) | Rule-level (`test_wrong_marker_many_descriptor_matches_but_bad_homography_is_rejected`) | Yes | Hand-built clustered point cloud, not real repeated-texture descriptors | reference grid cells, inliers |
| Invalid quadrilateral | Reject (`invalid_quad`) | Rule-level (`test_invalid_quadrilateral_is_rejected`) | No (pure geometry) | — | quad valid, corners |
| Low inlier ratio | Reject (`low_inlier_ratio`) | Rule-level (`test_low_inlier_ratio_is_rejected`) | No (pure geometry) | — | inliers, inlier ratio |
| Excessive reprojection error | Reject (`high_reprojection_error`) | Rule-level (`test_excessive_reprojection_error_is_rejected`) | No (pure geometry) | — | reprojection error |
| Two ambiguous candidates | Deterministic reject (`candidate_margin_too_small`) | Rule-level (`test_ambiguous_two_candidates_return_a_deterministic_rejection_code`) | Yes — needs two real printed markers side by side | Only tested via synthetic match-count pairs, not real ORB output on two real markers | good matches, matched_pair_id |
| Legacy full-image marker | Accept, unchanged behavior | Rule-level (`test_legacy_full_image_marker_passes`) | Yes | — | marker type |
| Stale generation/orientation response ignored | Ignored client-side | Source-assertion (`test_stale_generation_response_is_ignored`, plus `test_gate_jr_scanner_recovery.py`) | Yes (rotate device mid-scan) | Confirms the guard code exists, not that it fires correctly under real orientation-change timing on a real device | generation, orientation revision, stale/dropped count |
| Overlay disappearing during rotation (real-device observation) | Overlay recovers within grace window, no duplicate loop | Source-assertion only (grace timing, single-loop guards) | **Yes — this is the actual reported instability, needs real-device confirmation** | No automated test can reproduce real device rotation/sensor timing | tracking confidence, active loop count, recovery state |
| Perspective tilt reducing tracking (real-device observation) | Local tracking degrades gracefully, recovers or clears within grace window | Rule-level (perspective thresholds) + source-assertion (grace) | **Yes — real-device observation this pass was meant to investigate** | Synthetic perspective tests prove the threshold math; they cannot reproduce real optical-flow behavior under a moving phone | tracking confidence, consecutive rejected frames |

## Summary

- **20** automated tests in the robustness pack, all synthetic/generated — zero committed binary fixtures.
- Every rejection **code** this pass introduced (`insufficient_inliers`, `low_inlier_ratio`, `high_reprojection_error`,
  `clustered_reference_points`, `clustered_roi_points`, `invalid_quad`, `implausible_scale`, `excessive_perspective`,
  `stale_generation`, `stale_orientation`, `candidate_margin_too_small`) has at least one automated test reaching it,
  except `clustered_roi_points` and `implausible_scale`, which are near-duplicates of `clustered_reference_points`
  for any mathematically consistent homography (see the comment in `evaluate_homography_quality` in `app.py`) and
  `distorted_quad`'s aspect-specific sibling respectively — both reachable, neither easily forced independently.
- The two real-device observations that motivated this pass (overlay disappearing on rotation, perspective tilt
  reducing tracking) are **not** resolved by this pack — they need the manual phone checklist below.
