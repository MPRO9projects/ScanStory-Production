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

