---
title: Performance Budget
tags:
  - scan-story/release-1
  - performance
status: draft
---

# Performance Budget

Release 1 uses budgets as guardrails. Proposed SLO values must be validated with measurement before being treated as final.

## Budgets

- Dashboard JavaScript target: under 250 KB compressed where practical.
- Scanner engine never loads on creator dashboard pages.
- Scanner assets load only after viewer starts.
- Device mode controls asset weight.
- OpenCV/WASM load has timeout, retry, fallback, and diagnostics.
- Media variants are adaptive by device and network.
- Recognition endpoints have latency budgets and error budgets.
- Analytics must not block viewing.

## Rule

No new feature may increase scanner startup cost unless the viewer path actively needs it.

## Revision 1 Measurement Gates

Decision status: Approved Release 1 rule.

Measurement environments:

- local developer machine
- representative mid-range Android device
- representative iPhone
- staging environment
- load-test environment

Each budget must distinguish proposed target, measured baseline, acceptance threshold, and blocking regression threshold. Unmeasured proposed targets must not be reported as current performance.

Creator measurements: dashboard initial payload, Experience-list query latency, Trigger-list query latency, upload-response time, status-update latency, UI responsiveness at 30, 100, and 1,000 Triggers, and processing-job queue time.

Viewer measurements: shell visible, capability check, scanner dependency load, camera-ready time, first-recognition time, video-start time, tracking FPS, memory, recovery, and fallback load.

Backend measurements: detection p50/p95, database-query count, feature extraction duration, robustness-test duration, QR generation duration, queue age, and processing retry rate.
