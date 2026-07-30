# Animation And Rendering Audit

## Confirmed Findings

1. Landing page has unbounded animation loop after first mouse move.
   - Evidence: `templates/user/landing.html:2423` to `templates/user/landing.html:2445`.
   - The loop schedules `requestAnimationFrame(tick)` repeatedly and reads scroll position each frame.

2. Blog page repeats the same mouse/RAF pattern.
   - Evidence: `templates/user/blog.html:1091` to `templates/user/blog.html:1114`.

3. Multiple scroll listeners mutate transforms.
   - Evidence: `templates/user/landing.html:2407`, `templates/user/landing.html:2510`, `templates/user/landing.html:2571`, `templates/user/dashboard.html:791`.

4. Scanner uses continuous frame loop and server round trips for detection/tracking.
   - Evidence: `templates/user/scanner.html:737`, `templates/user/scanner.html:1084`, `templates/user/scanner.html:1222`.
   - Backend work is CPU-heavy OpenCV at `app.py:3262` and `app.py:3750`.

5. Video overlay preloads automatically.
   - Evidence: `templates/user/scanner.html:566` has `preload="auto"`.

## Bottleneck Classification

- Landing/blog smoothness: likely browser main-thread and painting.
- Scanner smoothness: mixed client CPU, camera frame processing, network latency, and backend CPU.
- Mobile risk: high, due OpenCV.js download, camera video, canvas capture, fetch loop, and video overlay.

## First Fixes To Approve Later

- Replace RAF blob parallax with CSS-only or IntersectionObserver-gated animation.
- Pause all offscreen videos and animations.
- Keep scanner detection frequency low and adaptive.
- Move CPU-heavy scanner matching out of general web workers into a bounded worker/process queue or dedicated service.

