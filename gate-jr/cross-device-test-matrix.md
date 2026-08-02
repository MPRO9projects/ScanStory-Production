# Cross-Device Scanner Test Matrix

Template for logging **real-device** scanner runs. This is a data-collection format, not a
report of results — no row here should be filled in from automated tests. See
`gate-jr/scanner-quality-matrix.md` for what automation actually covers; this document is
specifically for the manual evidence automation cannot produce.

Do not claim real-device or cross-device certification from automated tests alone. A device
row only counts as evidence once someone has actually run it on that device and filled it in.

## How to log a run

Copy the row template below into the results table for each test. One row per scenario
per device. Use the `?scanner_debug=1` diagnostics panel to read off generation/session
IDs, camera start/restart counts, scan-loop start counts, stale-response count, good
matches, inliers, and inlier ratio directly — don't estimate them.

### Row template

| Field | Value |
|---|---|
| Test ID | |
| Date/time | |
| Marker project ID | |
| Marker creation device | |
| Scanner device | |
| Browser/version | |
| OS | |
| Viewport | |
| devicePixelRatio | |
| Camera resolution | |
| Facing mode | |
| Frame rate | |
| Orientation | |
| Distance | |
| Viewing angle | |
| Lighting | |
| Glare | present / absent |
| Time to first valid detection | |
| Detection continuity while moving | |
| Marker-loss behavior | |
| Reacquisition time | |
| Good-match range | |
| Inlier range | |
| Inlier-ratio range | |
| Dominant rejection reason | |
| Camera start count | |
| Scan-loop start count | |
| Stale-response count | |
| Pass/fail | |
| Observations | |

## Scenarios to run

1. Marker created and scanned on Device A.
2. Device A's marker scanned on Device B.
3. Marker created and scanned on Device B.
4. Device B's marker scanned on Device A.
5. Samsung primary rear camera.
6. Samsung wide-field camera, if the browser selects it automatically.
7. Portrait orientation.
8. Landscape orientation.
9. Moderate tilt (~30-45°).
10. Temporary marker loss (move marker out of frame briefly, bring back).
11. Glare on the marker surface.
12. Low light.
13. Slow, deliberate movement while tracking.
14. Faster/casual movement while tracking.
15. Background the browser mid-scan, then foreground it again.

## Results

_(No rows recorded yet — this is a template only. Fill in one row per scenario per device
as real-device runs happen.)_

| Test ID | Date/time | Scanner device | Scenario | Camera start count | Scan-loop start count | Stale-response count | Pass/fail | Observations |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

## Known non-blocking asset gaps (deferred cleanup)

Real-device network logs during scanner testing show two unrelated 404s on every page load,
site-wide (not scanner-specific, not scanner-blocking):

- `GET /favicon.ico 404` — no favicon file exists anywhere under `static/`; the browser
  requests this implicitly on every page regardless of any `<link rel="icon">` tag.
- `GET /static/assets/og/scanstory-og.jpg 404` — several templates (`landing.html`,
  `projects.html`, `register.html`, `subscribe.html`, `contact.html`, `blog.html`,
  `blog_articles/article.html`, `edit_project.html`, `privacy_policy.html`, `terms.html`)
  reference an `og:image` pointing at this file, which does not exist under `static/assets/`.

Neither originates from `scanner.html`, which has no favicon/og-image reference of its own.
Fixing either requires adding a real image asset (no existing valid asset to point at
instead) and/or editing templates outside this pass's allowed-files list — out of scope
here per "do not expand scope to redesign assets." Logged as a later cleanup item: add
`static/favicon.ico` and `static/assets/og/scanstory-og.jpg`, or update the `og:image` tags
to an asset that already exists.

## What this matrix cannot tell you

- Whether the automated robustness pack (`gate-jr/scanner-quality-matrix.md`) numerically
  matches real ORB behavior on a given device's camera/lens/sensor.
- Whether the rotation/orientation fix in this pass (`isStreamDead()` replacing the flaky
  `isCameraHealthy()` restart check) actually eliminates the reported "Preparing Camera
  during ordinary marker loss" behavior on a real device — that can only be confirmed by
  scenario 9/10 above, watched directly, with the diagnostics panel open.
