---
title: Recognition Robustness Review
tags: [scan-story/release-1, readiness/vision]
status: draft
---

# Recognition Robustness Review

## Current Baseline

Current code extracts ORB features to `.npz`, quick-scores processed pairs, verifies with homography, returns corners, then tracks in browser with OpenCV optical flow.

## Required For Release 1

Lighting variation, low light, glare, blur, JPEG compression, scale, perspective, crop, mild occlusion, repetitive/blank regions, screen versus print targets, false-positive thresholds, similar-trigger warnings, large-Experience candidate retrieval, re-anchoring, target-loss recovery, and feature-version compatibility.

## Recommended Enhancements

Orientation/mirroring test matrix, print-size guidance, robustness score history, and project-scoped descriptor index.

## Experimental/Future

Any object/person/place/3D/WebXR recognition remains out of Release 1.

Recognition readiness score: **62/100**.

