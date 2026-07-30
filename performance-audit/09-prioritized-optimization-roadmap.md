# Prioritized Optimization Roadmap

## Priority 0 - Immediate Production Mistakes

1. Confirm production is not running `app.run(debug=True)`.
   - Evidence: `app.py:5768`.
   - Verify: process list and response headers/logs.

2. Stop disabling cache for landing videos.
   - Evidence: `app.py:1459`.
   - Impact: very high repeat-load improvement.

3. Move `db.create_all()` and bootstrap mutations out of request app import.
   - Evidence: `app.py:239`, `app.py:5762`.
   - Impact: safer deploys and faster startup.

4. Reduce or gate scanner debug prints in production.
   - Evidence: scanner logs across `app.py:3266` to `app.py:3606`.
   - Impact: lower I/O overhead and cleaner logs.

## Priority 1 - High-Impact Quick Wins

1. Compress/transcode videos, especially `static/videos/demo.mp4`.
2. Serve static/video assets with CDN and long-lived immutable cache headers.
3. Replace Tailwind CDN with compiled CSS.
4. Gate/offload landing/blog RAF animations.
5. Add pagination to admin users/payments/subscriptions/activity pages.

## Priority 2 - Structural Improvements

1. Move uploads/media to S3 or compatible object storage.
2. Move background processing to a queue worker.
3. Split CPU-heavy scanner work from general web requests.
4. Add explicit migrations.
5. Add real observability: structured logs, metrics, traces, alerts.

## Priority 3 - Reliability And Scale

1. Add reverse proxy/CDN.
2. Add health checks and graceful shutdown.
3. Add managed DB backups.
4. Add deploy rollback path.
5. Add autoscaling only after baseline metrics exist.

