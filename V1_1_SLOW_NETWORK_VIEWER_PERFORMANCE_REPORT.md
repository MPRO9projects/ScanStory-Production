# ScanStory V1.1 Slow-Network Viewer Performance Report

## 1. Starting HEAD

- Agent 2 branch: `agent/v1.1-experience-ux`
- Starting HEAD after fast-forward from integration: `29eb9faed96810016a6bcf22dfa0ae4e2704577d`
- Integration reference checked: `F:\ScanStory-main\ScanStory-integration`, `develop/scanstory-v1.1`, `29eb9faed96810016a6bcf22dfa0ae4e2704577d`

## 2. Ending HEAD

- Pre-commit working tree contains only the narrow delivery changes listed below.
- Final commit is recorded in the final response after commit.

## 3. Commits

- Pending at report creation.

## 4. Files Changed

- `templates/admin/base.html`
- `templates/admin/project_preview.html`
- `templates/user/project_preview.html`
- `templates/user/scanner.html`
- `tests/gate_jr/test_scanner_lifecycle.py`
- `tests/integration/test_admin_navigation_routing.py`
- `V1_1_SLOW_NETWORK_VIEWER_PERFORMANCE_REPORT.md`

## 5. Baseline Methodology

- Seeded a disposable local SQLite database under the Agent 2 worktree with one image-video scanner project and one Direct QR project.
- Started Flask locally on `127.0.0.1:5077` with the required venv Python and fake queue mode.
- Measured representative public/scanner routes with Playwright in Chrome and Edge.
- Used Chromium DevTools network emulation for `0.6 Mbps` and `0.3 Mbps` scanner checks.
- Removed the disposable `instance/` measurement artifact after measurement.

## 6. Baseline Route Measurements

Chrome, localhost, cold-ish browser context:

- `/`: 26 requests, main document 118 KB, largest resource `/media/card` at 5.48 MB.
- `/login`: 9 requests, 28 KB HTML, `login.png` 785 KB.
- `/register`: 7 requests, 46 KB HTML.
- `/contact`: 10 requests, load delayed by Google Maps iframe.
- `/scanner/1` image recognition: 7 requests, OpenCV JS 10.96 MB, scanner runtime 12.9 KB, target image 4.1 KB.
- `/scanner/2` Direct QR: 6 requests, no OpenCV request, video metadata request only.

Edge showed the same critical-path shape.

## 7. Scanner Critical-Path Findings

- Image-recognition scanner cold start is dominated by `/static/js/opencv.js`.
- Direct QR path avoids OpenCV and camera/recognition work.
- Scanner runtime JS is small and route-specific.
- Razorpay is not loaded by scanner routes.
- The first target guide image was lazy-loaded before this change, which can delay the immediate "what to scan" visual on weak connections.

## 8. OpenCV/WASM Findings

- `/static/js/opencv.js`: `Cache-Control: public, max-age=31536000, immutable`, size 10,963,702 bytes.
- `/static/js/opencv_js.wasm`: `Cache-Control: public, max-age=31536000, immutable`, size 3,338,276 bytes.
- Repeat scanner visits can reuse browser cache if deployment keeps the asset URL stable for compatible binaries.
- No OpenCV runtime behavior was changed.

## 9. Video Startup Findings

- Direct QR and fallback video were already `preload="metadata"`.
- AR overlay video was `preload="auto"`.
- Changed AR overlay video to `preload="metadata"` so source assignment does not invite full video transfer before playback.
- Creator/admin project preview videos now use `preload="metadata"`.

## 10. Fast-Start Findings

- Existing `compress_video()` already uses `movflags="+faststart"` for both stream-copy and compressed outputs.
- No media-processing code was changed.

## 11. Third-Party Script Findings

- Admin base loaded Chart.js globally from jsDelivr.
- No admin template in this worktree calls `Chart(...)` or `new Chart(...)`.
- Removed the unused global Chart.js script from admin base.

## 12. Razorpay Loading Result

- Razorpay remains limited to payment/add-on pages found during source audit:
  - `templates/user/subscribe.html`
  - `templates/user/profile.html`
  - `templates/user/project_preview.html`
- Razorpay was not found on scanner, dashboard, projects, ownership, or admin base routes.
- No payment code or business semantics changed.

## 13. Image/Thumbnail Findings

- Project preview pages render media in card-sized containers but use original media endpoints.
- No existing thumbnail derivative was found in this narrow pass.
- Added `loading="lazy"` and `decoding="async"` for creator/admin project preview images.
- Did not build a new thumbnail pipeline.

## 14. Static Caching Result

- OpenCV/WASM are long-lived immutable.
- Mutable scanner runtime and CSS remain `no-cache`, appropriate because filenames are not content-hashed.
- Production CDN/proxy should preserve immutable OpenCV headers and validate mutable CSS/JS via ETag/Last-Modified.

## 15. Compression Result

- Local Flask dev responses did not show `Content-Encoding`.
- Do not add Flask gzip here.
- Production reverse proxy/CDN should enable Brotli/gzip for HTML, CSS, JS, JSON, SVG, and WASM where supported; do not compress JPEG/MP4 redundantly.

## 16. API/Polling Result

- No polling changes made.
- Scanner recognition cadence, detection request behavior, and processing status APIs were left untouched.

## 17. Route-Specific Asset Result

- Removed one unused admin-wide third-party dependency.
- Scanner remains route-specific and does not load payment/admin libraries.

## 18. Font Result

- Public pages still use external fonts and icon CSS.
- No typography redesign or new font dependency added.

## 19. Lazy-Loading Result

- Scanner target guide now prioritizes only the first target image and lazies later targets.
- Creator/admin preview images are lazy/async.
- Preview videos are metadata-only.

## 20. Creator Performance Result

- Project preview pages no longer eagerly load full preview images below the fold.
- Preview videos no longer advertise eager preload.

## 21. Admin Performance Result

- Admin base no longer loads unused Chart.js globally.
- Admin project preview media uses lazy images and metadata-only video preload.

## 22. Scanner/Viewer Performance Result

- Cold OpenCV image-recognition byte cost is unchanged and expected.
- Direct QR remains the fastest viewer path and avoids OpenCV.
- Overlay video startup is less aggressive because preload is metadata-only.

## 23. 0.6 Mbps Result

- Chrome `/scanner/1` after change: 8 requests, no third-party requests, measured load about 6.2s in the reused-cache emulation context.
- Edge `/scanner/1` after change: 8 requests, no third-party requests, measured load about 6.2s in the reused-cache emulation context.
- Note: the browser reused cached OpenCV in these repeated same-context emulation checks; cold first-load on a real 0.6 Mbps network is still OpenCV-bound.

## 24. 0.3 Mbps Result

- Chrome `/scanner/2` Direct QR after change: 5 requests during emulated 0.3 Mbps check, load about 10.7s in the reused-cache context.
- Edge `/scanner/2` Direct QR after change: 5 requests during emulated 0.3 Mbps check, load about 10.7s in the reused-cache context.
- No OpenCV request on Direct QR.

## 25. Chrome Result

- `/scanner/1` normal after change: 7 requests, no third-party requests, OpenCV remains largest resource.
- `/scanner/2` normal after change: 6 requests, no third-party requests, no OpenCV.

## 26. Edge Result

- Edge mirrored Chrome: scanner routes had no third-party requests; image-recognition loaded OpenCV; Direct QR avoided OpenCV.

## 27. Focused Tests

- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\gate_jr\test_scanner_lifecycle.py tests\gate_jr\test_gate_jr_scanner_recovery.py -q`
  - First run hit tool timeout.
  - Rerun: `555 passed`.
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\contracts\test_scanner_contract.py -q`
  - `15 passed`.
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_admin_navigation_routing.py -q`
  - `30 passed`.
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\integration\test_fallback_pair_config_ui.py -q`
  - `14 passed`.
- `F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe -m pytest tests\security\test_security_health_performance.py -q`
  - `14 passed`.
- Jinja parse of touched templates: passed.

## 28. Console/Network Errors

- Browser route measurements reported HTTP 200 for measured public/scanner routes.
- No scanner static 404s appeared in measured waterfalls.
- A dedicated console-error capture was not performed beyond the request waterfall.

## 29. Scanner Hashes

Before and after SHA256:

- `scanner_runtime.py`: `A092B3F141F4E1CA743E45693DB5B3560843B86BAF59B853570607174982AF16`
- `static/js/scanner-runtime.js`: `95D5305DD3F8C1C0D1DB84CA90B51FE79B8BB322BF1B1A2A3E771C270B3EB7B3`

Both are unchanged.

## 30. Migration Status

- No models changed.
- No migrations created.
- No database schema changes.

## 31. git diff --check

- Passed. Git emitted line-ending warnings only.

## 32. git status --short

- Expected modified files only; disposable measurement artifacts were removed.

## 33. Before/After Summary

- Scanner recognition cold path: same bytes, safer target priority and video preload.
- Direct QR: same no-OpenCV architecture, metadata video preserved.
- Admin base: one global unused third-party request removed.
- Project previews: image/video loading made less eager.

## 34. Remaining Limitations

- OpenCV cold first-load remains the major weak-network cost.
- Public landing still loads a 5.48 MB card video and many images; not changed because this pass prioritized scanner/viewer/creator/admin surfaces and avoided broad redesign.
- Login page still includes a 785 KB image.
- No generated thumbnail pipeline exists; previews still use original media endpoints when visible.
- Production compression/CDN behavior must be configured outside Flask.

## 35. Next Recommendation

- Add content-hashed static asset filenames before extending long immutable caching beyond OpenCV/WASM.
- Add a real thumbnail derivative pipeline in a backend-owned storage/media package.
- Certify on physical low-end Android/iOS devices over throttled cellular, because localhost emulation cannot prove camera startup or real radio behavior.
