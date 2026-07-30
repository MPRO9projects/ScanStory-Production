---
title: Mobile And Device Strategy
tags:
  - scan-story/release-1
  - scanner
status: draft
---

# Mobile And Device Strategy

Scanner modes: Full, Standard, Lightweight, Unsupported.

```mermaid
stateDiagram-v2
  [*] --> PageLoaded
  PageLoaded --> CapabilityCheck
  CapabilityCheck --> ModeSelected
  ModeSelected --> ScannerAssetsLoading
  ScannerAssetsLoading --> CameraRequesting
  CameraRequesting --> CameraReady
  CameraReady --> RecognitionPreparing
  RecognitionPreparing --> ScannerReady
  CapabilityCheck --> UnsupportedBrowser
  ModeSelected --> Degraded
  ScannerAssetsLoading --> AssetLoadFailed
  CameraRequesting --> PermissionDenied
  CameraRequesting --> CameraFailed
  RecognitionPreparing --> NetworkFailed
  ScannerReady --> ExperienceUnavailable
```

## Capability Checks

Secure context, media devices, WASM, WebGL, memory, processor count, network, video codec support, browser, OS, and reduced-motion preference.

## State Guarantees

Every loading state has timeout, retry, message, fallback route, diagnostic code, and analytics event.

