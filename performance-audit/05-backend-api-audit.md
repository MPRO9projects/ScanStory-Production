# Backend API Audit

## Confirmed Findings

1. Import-time DB mutation happens before serving.
   - Evidence: `app.py:239` calls `db.create_all()` and bootstraps default data.
   - Risk: slow startup, unsafe deploys, duplicate bootstrapping, production schema drift.

2. Debug logging is enabled globally.
   - Evidence: `app.py:51` and `app.py:52`.
   - Many request-path prints exist, including `app.py:166`, `app.py:174`, and scanner logs across `app.py:3266` to `app.py:3606`.

3. CPU-heavy OpenCV work runs inside request handlers.
   - Evidence: `/detect_init` starts at `app.py:3262`; `/detect_track` starts at `app.py:3750`.
   - It reads uploaded frames, decodes images, computes ORB features, matches descriptors, computes homography, and returns JSON.

4. Upload processing uses in-process background threads.
   - Evidence: user upload thread at `app.py:2880`; admin upload thread at `app.py:5554`.
   - Risk: jobs die on process restart, duplicate under multi-worker deployments, no retry/queue visibility.

5. Upload size limits are large.
   - Evidence: `MAX_IMAGE_SIZE = 50 MB`, `MAX_VIDEO_SIZE = 1 GB` at `app.py:716` to `app.py:717`.
   - Risk: RAM, disk, request body, and worker exhaustion.

## Slow Endpoint Candidates

- `POST /detect_init`: CPU-bound and DB lookup heavy.
- `POST /detect_track`: CPU-bound per tracking request.
- `POST /upload`: disk writes plus image processing and background job startup.
- Admin list/report pages: many `.all()` queries and per-row counts.

## Measurement Plan

Use a staging database and run:

```powershell
curl -w "@curl-format.txt" -o NUL -s http://localhost:5000/
autocannon -c 10 -d 30 http://localhost:5000/
k6 run scanner-smoke.js
```

No production load test should run without explicit approval.

