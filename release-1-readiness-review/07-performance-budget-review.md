---
title: Performance Budget Review
tags: [scan-story/release-1, readiness/performance]
status: draft
---

# Performance Budget Review

## Evidence

Performance audit reports 57 MB demo video, 13.64 MB OpenCV assets, Tailwind CDN, repeated animation loops, OpenCV inside Flask request handlers, in-process daemon processing, startup DB mutation, local media storage, and unbounded admin lists.

## Feasibility

| Budget | Feasibility |
|---|---|
| Dashboard JS under 250 KB compressed | Aggressive, measurable locally |
| Scanner engine excluded from dashboard | Realistic |
| Scanner assets lazy after viewer start | Realistic but current SW caches OpenCV after scanner visit |
| OpenCV/WASM timeout/retry/fallback | Partially present; fallback insufficient |
| Adaptive media delivery | Not currently present |
| Recognition latency budget | Too vague; needs target and measurement |
| Analytics non-blocking | Not yet specified enough |

## Review Requirements

Creator: measure landing payload, dashboard payload, lists at 1/10/30/100/500/1000 triggers, upload responsiveness, polling, search/filter, batch retry, publication readiness.

Viewer: measure initial shell, scanner asset load, OpenCV/WASM, camera startup, first recognition, video startup, tracking FPS, memory, CPU, target-loss recovery, fallback load.

Backend: measure detection latency, DB queries, queue delay, feature extraction, quality tests, video processing, QR generation, admin list queries, analytics aggregation.

Performance readiness score: **52/100**.

