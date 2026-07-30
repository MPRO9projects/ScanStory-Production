# Frontend Performance Audit

## Confirmed Findings

1. Landing page is very large for a server-rendered template.
   - Evidence: `templates/user/landing.html` is 114,007 bytes.
   - It contains inline CSS, inline JS, many sections, videos, animations, and CDN dependencies in one route.

2. Tailwind is loaded from CDN in production-facing templates.
   - Evidence: `templates/user/landing.html:216`, `templates/user/blog.html:132`, `templates/user/dashboard.html:10`.
   - Risk: browser builds styles at runtime, adds network dependency, and prevents a compact compiled CSS bundle.

3. Third-party scripts block or add main-thread work.
   - Evidence: `templates/user/landing.html:218`, `templates/user/landing.html:2361`, `templates/user/blog.html:134`, `templates/user/blog.html:1055`.
   - Libraries: Vanilla Tilt, AOS, Font Awesome, Tailwind CDN.

4. Scanner loads OpenCV.js directly from local static JS.
   - Evidence: `templates/user/scanner.html:753`.
   - Asset size: `static/js/opencv.js` is 10.46 MB, `static/js/opencv_js.wasm` is 3.18 MB.

5. Large videos are present on the landing page.
   - Evidence: `templates/user/landing.html:1693` to `templates/user/landing.html:1708`, `templates/user/landing.html:1788`.
   - `demo.mp4` alone is 57.09 MB.

## Likely Impact

Most initial-load slowness is likely frontend/assets, not raw server size. A 2-core/4 GB server can still feel slow if it streams uncached 57 MB media, sends runtime Tailwind, and runs CPU-heavy OpenCV endpoints.

## Verification Needed

Run Lighthouse and Chrome Performance trace against production and local staging. Measure LCP, TBT, transferred bytes, and main-thread time for `/`, `/scanner/<id>`, `/dashboard`, and `/blog`.

