---
title: Mobile And Fallback Review
tags: [scan-story/release-1, readiness/mobile]
status: draft
---

# Mobile And Fallback Review

## Coverage

The specification names device modes and key failure classes. Current scanner has camera startup, OpenCV retries, visibility pause/resume, session end beacon, tracking loss/re-detection, and service-worker OpenCV caching.

## Gaps

Slow/unstable network, offline after initial load, camera selection, missing WASM, video codec failure, autoplay failure, orientation change, low memory, thermal throttling, incoming call, tab suspension, camera interruption, private/paused/archived Experience, accessibility, and reduced motion need explicit timeout/retry/message/fallback/diagnostic/event rows.

## Rule

No viewer state may end in an endless loading screen.

Mobile readiness score: **55/100**.

