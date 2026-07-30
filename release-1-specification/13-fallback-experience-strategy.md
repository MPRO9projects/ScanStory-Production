---
title: Fallback Experience Strategy
tags:
  - scan-story/release-1
  - fallback
status: draft
---

# Fallback Experience Strategy

Every published Experience requires fallback. Fallback is not an error page; it is the alternate way to experience the content.

## Fallback Types

- Direct video.
- Cover image with play button.
- Gallery.
- Trigger list.
- Webpage.
- Creator message.

## Fallback Triggers

Camera denied, unsupported browser, unsupported device, OpenCV/WASM load failure, poor network, target not found, viewer opt-out, degraded mode, paused Experience, or private/restricted failure.

## Metrics

Fallback launches are counted separately from recognition success. The creator needs to know when fallback saved a viewing session.

## Revision 1 Billing And Availability Rule

Decision status: Approved Release 1 rule.

A fallback launch counts as an Experience View because the viewer opened the Experience. Recognition attempts, detection frames, and re-anchor requests inside that session are not billable. Fallback must be reachable for unsupported devices, denied camera permission, scanner dependency failure, detection timeout, paused/unavailable policy, and media failure when policy permits.
