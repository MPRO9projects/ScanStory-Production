# Final Conclusive Verification — Shared Canvas, Tracking Coordinates, Overlay Shape Loss

Status: verification only. No production scanner behaviour changed. 8 new tests added to
`tests/gate_jr/test_scanner_lifecycle.py` (describe current behaviour only, no assertions
about desired future behaviour, no code under test modified to make them pass).

---

## 1. FINAL VERDICT

**A. PROVEN SHARED-CANVAS CORRUPTION RISK.**

The server-capture path (`detectOnceFromServer`) and the local-tracking path (`trackFrame`
→ `matFromVideoGray`) share exactly one `<canvas id="cap">` / 2D context pair, with no
mutual exclusion and no dimension-consistency guard. `detectOnceFromServer` resizes this
canvas to detection dimensions (`capW`×`capH`, ~800px) synchronously, before any `await`,
and does not restore it to tracking dimensions (`frameW`×`frameH`) unless that specific
request ends in an *accepted* detection. Every `trackFrame()` tick that runs during the
resulting drawImage/toBlob/fetch window (which the evidence already shows can span
200 ms–8 s, and which nothing pauses) draws and reads the canvas at whatever size it
currently holds — producing a `gray` Mat whose dimensions do not match `prevGray` or the
coordinate space of `prevPts`/`frameW`/`frameH`. This is provable directly from the
executable sequence, not inferred from timing alone.

---

## 2. CODE PROOF

- `const cap = document.getElementById("cap"); const ctx = cap.getContext("2d", { willReadFrequently: true });` — [scanner.html:1168-1169](templates/user/scanner.html#L1168-L1169). One canvas, one context, module-scoped — used by both paths below.
- `function matFromVideoGray()` — [scanner.html:2344-2352](templates/user/scanner.html#L2344-L2352): `ctx.drawImage(cam, 0, 0, cap.width, cap.height); ... ctx.getImageData(0, 0, cap.width, cap.height);` — always reads current `cap.width`/`cap.height`, no parameter, no size assertion.
- `detectOnceFromServer` capture-dimension resize — [scanner.html:2591-2600](templates/user/scanner.html#L2591-L2600): `capW`/`capH` computed from `DETECT_SIZE = 800` and `cam.videoWidth/videoHeight`, then `cap.width = capW; cap.height = capH;` — runs synchronously at function start, before `ctx.drawImage`/`cap.toBlob` (2611-2614) and before `await fetch` (2679).
- No restoration between resize and response: the only `cap.width = frameW` assignments are at [scanner.html:2772-2773](templates/user/scanner.html#L2772-L2773) and [scanner.html:2849](templates/user/scanner.html#L2849), both strictly *after* `const data = await r.json();` (2683) and only reached inside the accepted-detection branch (past the `!data.detected` early return at 2735-2747, the stale-generation/frame-size/orientation early return at 2711-2720, and the `poseCompatibility` rejection early returns at 2800-2812).
- `trackFrame()` — [scanner.html:2445-2568](templates/user/scanner.html#L2445-L2568) — never references `detectInFlight`, `activeDetectionController`, `capW`, or `capH`; its `requestAnimationFrame(trackFrame)` reschedule (2451) happens unconditionally at the top of every tick, before the `tracking`/`cvReady` guard, so the loop keeps ticking through the entire capture/fetch window regardless of what `detectOnceFromServer` is doing to the shared canvas.
- `cv.calcOpticalFlowPyrLK(prevGray, gray, prevPts, nextPts, ...)` — [scanner.html:2463](templates/user/scanner.html#L2463) — OpenCV's LK implementation requires `prevImg`/`nextImg` to be equal-sized Mats (a documented invariant of the algorithm, enforced as a C++ assertion in the OpenCV video module that `calcOpticalFlowPyrLK` wraps); `opencv.js`'s Emscripten binding surfaces a resulting assertion failure as a thrown JS value.
- The catch that would receive that exception — [scanner.html:2564-2567](templates/user/scanner.html#L2564-L2567): `catch (e) { console.error('Track frame error:', e); tracking = false; }` — confirmed by `test_trackframe_catch_block_bypasses_droptracking_and_geometry_clear` to call neither `dropTracking()`, `clearTrackingGeometry()`, nor `requestPoseHold()`.
- Server's own `frameW`/`frameH` almost never equal `capW`/`capH`: `capW` is derived from `DETECT_SIZE = 800` (2590), while `frameW = Number(data.frame_width)` (2770) reflects the server's own working-JPEG dimension (up to `ORB_MAX_DIM`, typically 1200) — so when a mismatch occurs it is a genuine, non-coincidental dimension mismatch, not a near-miss that happens to still work.

---

## 3. CANVAS INVENTORY

| Item | Value |
|---|---|
| DOM ID | `cap` (single `<canvas>` element) |
| Variable | `cap` — [scanner.html:1168](templates/user/scanner.html#L1168) |
| Context variable | `ctx` — `cap.getContext("2d", { willReadFrequently: true })` — [scanner.html:1169](templates/user/scanner.html#L1169) |
| Purpose | Dual-use: (a) JPEG frame capture buffer for `/detect_init` uploads, (b) grayscale source buffer for local Lucas-Kanade tracking |
| Width/height init | Not set at declaration (defaults to the HTML attribute value / 300×150 canvas default until first assignment) |
| Width assignments | `cap.width = capW` (2599, detect-capture start); `cap.width = frameW` (2772, accepted-detection branch); `cap.width = frameW` again (2849, immediately before `prevGray = matFromVideoGray()`) |
| Height assignments | `cap.height = capH` (2600); `cap.height = frameH` (2773); `cap.height = frameH` (2849) |
| `drawImage` calls | 2611 (`detectOnceFromServer` capture) and 2345 (`matFromVideoGray`, called from both `trackFrame` and the accepted-detection branch's `prevGray = matFromVideoGray()` at 2850) |
| `getImageData` calls | 2346, inside `matFromVideoGray` only |
| OpenCV Mat creation from canvas | `cv.matFromImageData(imgData)` inside `matFromVideoGray` (2347) — the sole point where canvas pixels become a Mat |
| `toBlob`/`convertToBlob` calls | `cap.toBlob(res, "image/jpeg", 0.85)` (2614) — the only encode call; no `OffscreenCanvas`/`convertToBlob` exists anywhere in this file |
| Read by local tracking? | Yes — every `trackFrame()` tick via `matFromVideoGray()` |
| Written by server capture? | Yes — every `detectOnceFromServer()` call, at the very start |
| Used by overlay rendering? | No — `applyWarp` reads `frameW`/`frameH` module-level variables directly (see §7 below), never `cap.width`/`cap.height`; the overlay is a separate `<video id="overlay">` element with its own CSS transform, entirely independent of the `cap` canvas |

No second canvas or context exists anywhere in the file (`getElementById("cap")` and
`cap.getContext(` each occur exactly once — confirmed by
`test_capture_and_tracking_share_one_canvas_and_context`).

---

## 4. DIMENSION-MUTATION PROOF

| # | Assignment | Function | Why | Tracking possibly active? | rAF can run first? | Restored after? | Backing store reset? | Context state reset? | Prior pixels destroyed? | Tracker coords still old-size? | Next gray frame at new size? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `cap.width=capW; cap.height=capH` (2599-2600) | `detectOnceFromServer`, unconditional at start | Downscale to ≤800px/1200px for upload, avoid oversized POST | **Yes** — detect cycles run continuously (re-anchor/re-scan) regardless of `tracking` state | **Yes** — happens before any `await`, so any `trackFrame` rAF callback already queued or scheduled during the following `await` (toBlob, fetch) runs with `cap` at this size | Only if this specific request is later accepted (assignment 2/3 below); otherwise **never**, until the *next* detect cycle re-applies the same value | Per HTML canvas spec, any width/height assignment clears the bitmap and resets the backing store | Per spec, 2D context state (transform, etc.) is reset on resize — irrelevant here since no transform state is used on `ctx` | Yes — canvas contents cleared on resize; irrelevant since `matFromVideoGray` always redraws from `cam` before reading | Yes — `prevPts` was produced in whatever space the last accepted detection or last `trackFrame` tick used (`frameW`×`frameH`), unaffected by this resize | Yes — `matFromVideoGray`, if called next, draws at `capW`×`capH`, not `frameW`×`frameH` |
| 2 | `cap.width=frameW; cap.height=frameH` (2772-2773) | `detectOnceFromServer`, accepted-detection branch only | Prepare a consistent frame for the upcoming tracking reset | Yes, briefly (main thread, synchronous) | N/A — synchronous, no yield before assignment 3 | Superseded immediately by assignment 3 (same values, redundant but harmless) | Same as above | Same as above | Yes, immediately overwritten by the redraw at assignment 3 | N/A — reset happens right after | N/A |
| 3 | `cap.width=frameW; cap.height=frameH` (2849) | `detectOnceFromServer`, immediately before `prevGray = matFromVideoGray()` | Guarantee `prevGray` is captured at the exact size the new `prevPts` (server `init_points` or grid-sampled corners) are expressed in | Yes | N/A — synchronous | This IS the restoration point | Same as above | Same as above | Yes, replaced by the fresh `matFromVideoGray()` call two lines later | N/A — `prevPts` is (re)built in this same branch, at this same size | Yes — this call defines the new baseline size |

**Conclusion:** the canvas is only ever guaranteed to be at `frameW`×`frameH` (the tracking
coordinate space) for the instant right after an *accepted* detection resets it. From that
point until the *next* detect cycle starts, it stays correct and `trackFrame` ticks are
self-consistent (`prevGray`/`gray` both `frameW`×`frameH`, updated together each tick — see
§5, assignments in `trackFrame` itself never touch `cap.width`/`height`, only `matFromVideoGray`
reads it). The instant the next periodic detect cycle begins (line 2599), the canvas flips to
`capW`×`capH` and stays there — for the full duration of that request — unless/until that
specific request also happens to end in acceptance. A "no detection" or stale/rejected
response (by far the most common outcome while already tracking) leaves the canvas
permanently at `capW`×`capH` from that point forward.

---

## 5. COORDINATE-SPACE TABLE

| VALUE | SOURCE SPACE | TARGET SPACE | SCALE APPLIED | CODE LOCATION |
|---|---|---|---|---|
| `data.frame_width`/`data.frame_height` (server) | Server's own working-JPEG dimensions (up to `ORB_MAX_DIM`) | Client `frameW`/`frameH` | None — assigned directly | scanner.html:2770-2771 |
| `data.corners` (server) | Same server working-JPEG space as `frame_width`/`frame_height` | Client `newCorners` (via `normalizeCornerOrder`) | None — server coordinates used as-is, order-normalized only | scanner.html:2775-2776 |
| `data.init_points` (server) | Same server working-JPEG space | Client `prevPts` (Mat) | None — used as-is | scanner.html:2843, 2852-2855 |
| `capW`/`capH` (client) | Derived from `cam.videoWidth`/`videoHeight` scaled to `DETECT_SIZE=800`, capped at 1200 | Upload-only (`source_frame_width`/`source_frame_height` form fields) | Independent scale from `frameW`/`frameH` — **not the same space** | scanner.html:2591-2600, 2642-2643 |
| `prevGray` (Mat) | Whatever `cap.width`/`cap.height` were at the moment it was captured — `frameW`×`frameH` immediately after an accepted detection (2850), but ANY size if captured mid-`trackFrame` while `cap` was left at `capW`×`capH` by a concurrent detect cycle | N/A (raw pixel buffer) | N/A | scanner.html:2549-2550, 2850 |
| `gray` (Mat, current tick) | Current `cap.width`/`cap.height` at time of the `matFromVideoGray()` call — **can differ from `prevGray`'s size** per §4 | N/A | N/A | scanner.html:2344-2352, 2458 |
| Tracked points (`nextPts`/`goodNext`) | Whatever space `gray` was captured in this tick | Becomes `prevPts` for the next tick | None — carried forward as-is | scanner.html:2463-2473, 2552 |
| `newCorners` (from `cv.perspectiveTransform`) | Same space as `currCorners`/tracked points (assumed `frameW`×`frameH`, but genuinely `capW`×`capH`-space if corruption occurred) | Bounds-checked against `frameW`/`frameH` directly | **Space mismatch if corruption occurred** — bounds check uses stale reference dims against possibly-different-space coordinates | scanner.html:2504-2524 |
| `currCorners` (accepted, server-response path) | `frameW`×`frameH` (server space) | Overlay CSS space | Explicit conversion via `convertBackendCornersToOverlay(cornersFrame, frameW, frameH, ...)` | scanner.html:2180 |
| Overlay CSS transform | Output of `convertBackendCornersToOverlay`/`smoothPoseCorners`/`quadToMatrix3d` | `matrix3d(...)` applied to `overlayWrap` | Full explicit conversion, always using `frameW`/`frameH` — **never `cap.width`/`cap.height`** | scanner.html:2180-2295 (applyWarp) |

**Key distinction proven:** the *server-response render path* (`applyWarp`) is safe from
this hazard — it always uses the module-level `frameW`/`frameH` variables, never
`cap.width`/`cap.height` (confirmed by `test_applywarp_and_render_path_use_frameW_frameH_not_cap_dimensions`).
The corruption is confined to the *local optical-flow tracking path*
(`matFromVideoGray`/`calcOpticalFlowPyrLK`), which has no independent coordinate reference
and trusts whatever the shared canvas currently holds.

---

## 6. CONCURRENCY PROOF

Exact timeline, based on the code read above (single-threaded JS event loop — steps marked
"can interleave" are ordinary task/microtask-queue behavior, not speculative):

1. Local tracking active; `prevGray`/`prevPts` are `frameW`×`frameH`-space (last reset at an
   accepted detection, scanner.html:2849-2855).
2. `trackFrame`'s `requestAnimationFrame(trackFrame)` reschedule (2451) is already queued for
   the next paint.
3. The independent scan-timer chain (`scheduleNextScan`/`scanTick`) fires and calls
   `detectOnceFromServer()` (this happens regardless of `tracking` state — re-anchor/re-scan
   continues while already tracking, matching the supplied evidence "server scanning
   continues while overlay already playing").
4. `detectOnceFromServer` synchronously sets `cap.width = capW; cap.height = capH;` (2599-2600)
   — **before any `await`**.
5. `ctx.drawImage(cam, ...)` (2611) draws the live camera frame into the now-resized canvas;
   `cap.toBlob(...)` (2614) begins asynchronous JPEG encoding — this `await` yields the
   main thread.
6. **While `toBlob`'s callback is pending (proven up to 6-8s per the already-established
   evidence), the queued `trackFrame` rAF callback from step 2 fires.** Nothing in
   `trackFrame` (confirmed empty of `detectInFlight`/`activeDetectionController` checks —
   `test_track_frame_has_no_capture_in_flight_guard`) defers or skips this tick.
7. `trackFrame` calls `matFromVideoGray()` (2458), which draws/reads at the **current**
   `cap.width`/`cap.height` — i.e. `capW`×`capH` from step 4, not `frameW`×`frameH`.
8. `cv.calcOpticalFlowPyrLK(prevGray [frameW×frameH], gray [capW×capH], prevPts [frameW×frameH-space], ...)`
   is called with a genuine, non-coincidental Mat-size mismatch.

**Answers:**
- Can steps 7 and 8 occur while `cap` remains in capture dimensions? **Yes — proven, not theoretical** (§6 steps 4-8, backed by `test_detect_resizes_shared_canvas_before_network_await` and `test_capture_dimensions_are_not_restored_before_response_arrives`).
- Does `trackFrame` see the server-capture image instead of its expected tracking frame? **No** — `matFromVideoGray` always redraws fresh from `cam` (the live `<video>`), so pixel *content* is always current; the corruption is dimension/coordinate-space only, not stale image content.
- Can `prevGray` and `gray` have different sizes? **Yes — proven** (§4, §6).
- Can existing optical-flow points exceed the new matrix bounds? **Yes** — `prevPts` coordinates in `frameW`×`frameH` space can exceed a smaller `capW`×`capH` buffer's bounds.
- Can scale conversion be applied twice or not at all? **Not at all** — no scale conversion exists anywhere in the local-tracking path; it assumes a single implicit shared space that the shared canvas does not guarantee.
- Can this directly trigger the six `dropTracking` reasons? **See §7.**

---

## 7. SHAPE-LOSS CONNECTION

| REASON | CAN SHARED CANVAS CAUSE IT? | EXACT MECHANISM | CONFIDENCE |
|---|---|---|---|
| `insufficient_flow_points` | Yes | Mismatched-size Mats make most/all tracked points fail LK's internal bounds/pyramid checks → `status[i] !== 1` for most points → falls below `MIN_GOOD_POINTS` | High |
| `homography_empty` | Yes | Even if some points survive LK on a mismatched pair, their relationship no longer reflects real camera motion — `cv.findHomography` with RANSAC can find no consistent inlier set → `H.empty()` | Medium-high |
| `corner_order_invalid` | Yes | A homography computed from spatially-corrupted correspondences can project `currCorners` into a degenerate/flipped quad that `normalizeCornerOrder` rejects | Medium |
| `out_of_bounds` | Yes | Projected corners bounds-checked against `frameW`/`frameH` (2518-2519) while actually expressed in `capW`×`capH`-derived space → false "left the frame" verdict | High |
| `pose_rejected_<reason>` | Yes | `poseCompatibility` (area/center/corner/edge/diagonal-ratio checks, 2320-2341) compares a corrupted-space quad against the previous good quad — virtually guaranteed to trip one of these ratio limits | High |
| `tracking_geometry_invalid` | Yes | `applyWarp(currCorners)` (called with no `context`, so skips the response-staleness checks but still runs `isOverlayFrameQuadRenderable(cornersFrame, frameW, frameH)`) rejects a corrupted-space quad against the real `frameW`/`frameH` | High |
| *(uncaught path, not a `dropTracking` reason at all)* | Yes | If `cv.calcOpticalFlowPyrLK` itself throws on the size mismatch (OpenCV's documented equal-size invariant), the outer `catch` (2564-2567) sets `tracking=false` directly — **bypassing all six reasons above entirely, with no `[TRACK LOST]` diagnostic logged** | High — confirmed by `test_trackframe_catch_block_bypasses_droptracking_and_geometry_clear` |

No thresholds were changed or evaluated for adjustment — this table only maps an existing,
proven mechanism onto existing, unmodified reasons/thresholds.

---

## 8. VISIBLE OVERLAY-LOSS PATH

Answer: **G — one or more of the above, and the exact combination depends on which failure
path fires.**

- **Normal `dropTracking()` failures (the 6 named reasons):** `dropTracking` →
  `clearTrackingGeometry(reason, {holdPose:true})` → `tracking=false`, `currCorners=null`,
  `prevGray`/`prevPts` freed, then `requestPoseHold(reason)` (since `holdPose:true`) —
  **(D) tracking state false + (C) geometry cleared**, but the overlay is not immediately
  hidden: `requestPoseHold` only fades `overlayWrap.style.opacity` to `0.72` and does
  **not** call `overlay.pause()` — confirmed by `test_pose_hold_keeps_video_playing_only_stop_overlay_pauses_it`.
  The video keeps playing (matches the supplied evidence `ended:false, paused:false,
  loop:true`) for up to `POSE_HOLD_MS`, after which, if still not re-tracking,
  `overlayWrap.style.opacity` fades to `0` and `stopOverlayImmediate()` runs 140ms later —
  **only that final step is (A) video actually paused + (B) overlay hidden
  (`overlayWrap.style.display = "none"`)**.
- **The uncaught-exception path (§7's 7th row):** only **(D)** — `tracking=false` — fires.
  Geometry is *not* cleared (`currCorners` remains set to its last value), the overlay
  transform is *not* reset, and neither `requestPoseHold` nor `stopOverlayImmediate` runs.
  The overlay is left fully visible, fully opaque, and *playing*, frozen at the last
  successfully-applied quad, until the **next** `detectOnceFromServer` cycle's
  `!data.detected` branch (2735-2747) notices `!tracking` and finally calls
  `requestPoseHold('no_detection')` — which is the first point any visible change occurs.
  This is a distinct, previously-undiagnosed "detach" pattern: the card overlay stops
  following the card (frozen geometry) while remaining visible and playing, for an interval
  bounded only by however long until the next detect cycle's no-detection branch runs —
  matching the user-reported "pause/disappear/**detach**/resume" sequence more precisely
  than the named `dropTracking` paths alone.
- Can the video continue playing invisibly after geometry is cleared? **Yes, but only
  during the pose-hold fade window** (opacity 0.72 → 0, video still playing, not yet
  paused) — never "invisibly" in the sense of `display:none` while still playing, since
  `stopOverlayImmediate()` (the only path that sets `display:none`) also always pauses the
  video in the same call.

---

## 9. REQUIRED FUTURE FIX

Identification only — **not implemented this task**.

- **Separate capture canvas: required.** The single shared `cap` is the direct, proven
  mechanism (§1-6). A second, independent canvas dedicated to `matFromVideoGray`/tracking
  would eliminate the size-mutation interaction entirely, independent of any encode-path
  change.
- **Separate tracking canvas: required** (same item as above — tracking needs its own
  canvas, not merely a differently-named alias of the same element).
- **Fixed tracking dimensions: required.** The tracking canvas should be sized once (e.g.
  to `frameW`×`frameH` at acquisition) and never resized by any other code path for the
  duration of a tracking session — removing the possibility of a `prevGray`/`gray` size
  mismatch by construction.
- **No resize of tracking canvas during local tracking: required** — corollary of the
  above; if the tracking canvas must ever change size (e.g. on a genuinely new accepted
  detection with different `frameW`/`frameH`), it must do so atomically with the
  `prevGray`/`prevPts` reset, exactly as the current code already does correctly at
  scanner.html:2845-2855 — that reset pattern is sound; it is the *shared, externally-mutated*
  canvas in between resets that is the defect.
- **Frame-gap reset: required**, as a defense-in-depth complement — even with separate
  canvases, an explicit dimension-consistency assertion immediately before
  `calcOpticalFlowPyrLK` (comparing `prevGray.size()`/`gray.size()`) would convert any
  future regression of this kind into an explicit, diagnosed `dropTracking` reason instead
  of an uncaught/silently-bypassed exception.
- **Explicit valid/weak/lost states:** not required to fix *this specific* hazard (the
  existing implicit three-state model, per the architecture report already on file, is
  adequate for the geometry-quality checks it already performs) — but formalizing it
  remains a reasonable complementary improvement already covered in the prior architecture
  report.
- **Worker encoding fast path:** not required to fix *this specific* hazard — this
  corruption is caused by shared *dimensions*, not by main-thread encode latency per se.
  However, moving `toBlob` off the shared canvas (e.g. into a Worker with `OffscreenCanvas`,
  per the prior architecture report) would incidentally also remove this hazard, since the
  detect-path would no longer touch `cap` at all.
- **Hardened baseline fallback:** already covered by the prior architecture report;
  unaffected by this verification.

---

## 10. TESTS RUN

`python -m pytest tests/gate_jr/test_scanner_lifecycle.py -q` → **139 passed** (131 previously
+ 8 new proof tests added this task: `test_capture_and_tracking_share_one_canvas_and_context`,
`test_detect_resizes_shared_canvas_before_network_await`,
`test_capture_dimensions_are_not_restored_before_response_arrives`,
`test_track_frame_has_no_capture_in_flight_guard`,
`test_matframevideogray_has_no_fixed_dimension_or_size_guard`,
`test_trackframe_catch_block_bypasses_droptracking_and_geometry_clear`,
`test_pose_hold_keeps_video_playing_only_stop_overlay_pauses_it`,
`test_applywarp_and_render_path_use_frameW_frameH_not_cap_dimensions`).

`python -m pytest tests/gate_jr/test_gate_jr_scanner_recovery.py -q` → **88 passed**
(unchanged — this file was not touched this task).

Node smoke test: not run — `templates/user/scanner.html` was not modified this task (only
the test file was), so per the task's own conditional wording ("if files were touched")
this check does not apply.

`app.py` syntax check: not applicable — `app.py` untouched.

---

## 11. `git diff --check`

Run in `F:\ScanStory-main\ScanStory-integration`. Output: empty — no whitespace errors.

## 12. `git status --short`

```
 M app.py
 M templates/user/scanner.html
 M tests/gate_jr/test_gate_jr_scanner_recovery.py
 M tests/gate_jr/test_scanner_lifecycle.py
?? gate-jr/scanner-architecture-audit-and-research.md
?? gate-jr/shared-canvas-coordinate-verification.md
```

`app.py` and `scanner.html` show as modified from the prior tasks in this session (ROI
rollback, capture/watchdog/timer audit) — **not from this task**, which only added tests to
`tests/gate_jr/test_scanner_lifecycle.py` and this new report file.

## 13. Explicit confirmation

- **No production fix implemented.** Only `tests/gate_jr/test_scanner_lifecycle.py`
  (8 new tests, describing current behaviour only) and this markdown report were added
  this task. `templates/user/scanner.html` and `app.py` are unchanged from the end of the
  prior task.
- **Nothing staged.**
- **Nothing committed.**
