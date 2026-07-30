---
title: Scanner Contract Review
tags: [scan-story/release-1, readiness/scanner]
status: draft
---

# Scanner Contract Review

## Current Contract

Route: `/scanner/<project_id>` renders `templates/user/scanner.html`.

Detection route: `/detect_init` form-data payload includes `project_id`, `test_image`, and `scan_session_id`. Response on success includes `detected`, `matched_pair_id`, `video_url`, `corners`, `init_points`, `frame_width`, `frame_height`, `variant`, `inliers`, `top_checked`, `scan_session_id`, `ready_pairs`, `total_pairs`, and `is_admin_project`.

Tracking route: `/detect_track` form-data payload includes `project_id`, `pair_id`, `test_image`, `scan_session_id`. Response includes `ok`, `corners`, `frame_width`, `frame_height`, `variant`, and `inliers`.

Session end route: `/api/scanner/session/end` counts one successful user scan when a `ScanLog` for `scan_session_id` is successful and not counted.

## Current Risks

- Endpoints are unversioned.
- Scanner JS parses exact field names.
- Video URLs differ for user and admin projects.
- Feature lookup depends on project ID and pair index.
- OpenCV/WASM startup is client-side and heavy.
- Service worker only caches OpenCV assets.
- Current scanner forces `user_id` from QR query into session for scan counting.

## Compatibility Approach

Legacy scanner contract remains available -> new versioned scanner contract is introduced -> migrated Experiences opt into the new contract -> legacy QR links continue routing correctly.

| Area | Status |
|---|---|
| Current QR compatibility | At risk unless legacy route preserved |
| Scanner API compatibility | At risk unless `/detect_init` and `/detect_track` remain |
| Feature-artifact compatibility | Moderate risk |
| Media-path compatibility | High risk during storage abstraction |
| Scan-log compatibility | Moderate risk |

Scanner compatibility score: **58/100**.

