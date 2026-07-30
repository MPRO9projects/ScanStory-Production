# Final Audit Summary

## 1. Executive Summary

ScanStory is slow mostly because heavy frontend/media work and CPU-heavy scanner processing sit inside a small Flask monolith. Confirmed risks include a 57 MB demo video, 13.64 MB OpenCV client assets, Tailwind CDN runtime loading, continuous animation loops, Flask routes doing OpenCV work per request, local media storage, disabled video caching, and no production deployment config in repo.

## 2. Current Architecture

Browser -> Flask/Jinja -> local static/CDN assets -> Flask API -> SQLAlchemy/MySQL -> local filesystem media/features/QR -> Razorpay/reCAPTCHA/SMTP.

## 3. Top 10 Confirmed Bottlenecks

1. `static/videos/demo.mp4` is 57.09 MB.
2. `app.py:1459` sets landing video cache to `no-store`.
3. `static/js/opencv.js` is 10.46 MB and `opencv_js.wasm` is 3.18 MB.
4. `templates/user/landing.html:216` uses Tailwind CDN.
5. `templates/user/landing.html:2423` to `templates/user/landing.html:2445` runs a repeated RAF loop.
6. `templates/user/scanner.html:1222` posts frames to `/detect_init`.
7. `app.py:3262` and `app.py:3750` run OpenCV work inside request handlers.
8. `app.py:2880` and `app.py:5554` use in-process daemon threads for background processing.
9. `app.py:239` and `app.py:5762` run `db.create_all()`.
10. Admin list routes use unbounded `.all()` queries, for example `app.py:4271`, `app.py:4768`, `app.py:4927`.

## 4. Frontend Versus Server Responsibility

Estimate from repo-only evidence:

- Frontend/assets: 45 percent.
- Backend/scanner CPU: 30 percent.
- Database/query patterns: 10 percent.
- Deployment/server config: 15 percent.

## 5. Can Current Server Be Optimized?

Yes, probably for low traffic if production is configured correctly and the largest asset/cache/scanner issues are fixed. It may still struggle with concurrent scanner users because OpenCV work is CPU-bound.

## 6. Must-Fix Before AWS

- Confirm production is not using Flask debug server.
- Cache/compress/offload videos.
- Move media to CDN/object storage or at least serve via reverse proxy with range/cache headers.
- Compile CSS instead of Tailwind CDN.
- Remove startup DB mutation from app import.
- Add baseline measurements.

## 7. Recommended AWS Architecture

Preferred: CloudFront + S3 for static/media, ALB or reverse proxy to Flask app on EC2/ECS, RDS MySQL, CloudWatch logging, and a worker queue when upload processing grows.

## 8. Recommended Initial AWS Configuration

- App: EC2 t4g.medium or equivalent ECS task, 2 vCPU, 4 GB RAM.
- Storage: S3 for media/static uploads.
- CDN: CloudFront.
- DB: RDS MySQL small burstable class after DB size is known.
- Load balancer: optional for first low-cost phase, recommended for reliable production.
- Autoscaling: after metrics, not before.

## 9. Estimated Monthly Cost

Assumptions: AWS ap-south-1, Linux, on-demand, 730 hours/month, low traffic, 50-100 GB storage, modest bandwidth.

- Cost-conscious: about $25-$80/month.
- Reliable production: about $100-$250/month.
- Scalable: about $250-$700+/month.

## 10. Migration Risk

Medium. Main risk is local filesystem coupling and scanner CPU behavior. Risk drops after media is moved to object storage, DB migrations are explicit, and performance baselines exist.

## 11. Expected Improvement

Not guaranteed without tests. Likely gains:

- first load: high after video/CSS/cache fixes.
- repeat load: very high after cache fixes.
- API response: moderate after production server and query fixes.
- animation smoothness: moderate to high after RAF/video fixes.
- concurrent users: high only after scanner CPU and workers are isolated.
- reliability: high after managed DB, logs, backups, and health checks.

## 12. Recommended Next Implementation Phase

1. Run baseline tests.
2. Fix cache headers and video compression.
3. Confirm production server command.
4. Add pagination to admin lists.
5. Move DB bootstrap to migration/admin command.
6. Design worker/object-storage split.
7. Build AWS staging.

