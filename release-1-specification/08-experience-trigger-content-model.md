---
title: Experience Trigger Content Model
tags:
  - scan-story/release-1
  - data-model
status: draft
---

# Experience Trigger Content Model

An Experience is a versioned container for one or more triggers. Each trigger maps a reference image to a video and fallback behavior.

## Trigger Fields

- Reference image asset.
- Video content asset.
- Trigger settings.
- Processing status.
- Recognition-quality summary.
- Recognition artifacts.
- Fallback behavior.
- Published manifest entry.

## Content Rules

- Original uploads are preserved when allowed by plan and policy.
- Optimized variants are generated for device-appropriate delivery.
- Recognition artifacts are versioned by algorithm and processing version.
- Published versions are immutable.
- Editing a published Experience creates a new draft version.

