---
title: Analytics And Observability
tags:
  - scan-story/release-1
  - analytics
status: draft
---

# Analytics And Observability

## Creator Analytics

Experience Views, recognitions, fallback launches, time to recognition, failed attempts, device/browser/OS, privacy-safe geography, trigger performance, play completion, scanner errors, bandwidth, and popular Experiences.

## Operations Analytics

Processing time, failure rate, queue age, detection latency, device compatibility, OpenCV/WASM failures, camera-denied rate, QR errors, publication failures, and entitlement denials.

## Event Principles

- Use scanner session ID.
- Avoid raw camera frames.
- Sanitize diagnostics.
- Version event schemas.
- Do not block viewer flow on analytics.

