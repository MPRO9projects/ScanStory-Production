# Assets Audit

## Top Issues

1. `static/videos/demo.mp4` is 57.09 MB.
   - Used by landing demo video at `templates/user/landing.html:1788` to `templates/user/landing.html:1792`.

2. Landing video route disables caching.
   - Evidence: `app.py:1459` sets `Cache-Control: no-store`.
   - This makes repeat loads pay the media cost again.

3. OpenCV assets total 13.64 MB.
   - `static/js/opencv.js`: 10.46 MB.
   - `static/js/opencv_js.wasm`: 3.18 MB.
   - Service worker attempts caching at `static/sw.js:4` to `static/sw.js:49`.

4. Several PNG logo/step images are near 1 MB each.
   - `static/assets/logos/step2.png`: 0.97 MB.
   - `static/assets/logos/step3.png`: 0.94 MB.
   - `static/assets/logos/steeep1.png`: 0.86 MB.

## Largest Assets

See `largest-assets.csv`.

## Required Next Measurements

- Browser Network panel: transferred bytes after compression.
- Real displayed dimensions for each image/video.
- Cache headers from production, not only Flask source.
- Whether reverse proxy/CDN overrides `Cache-Control`.

