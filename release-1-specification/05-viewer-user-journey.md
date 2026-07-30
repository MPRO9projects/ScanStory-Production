---
title: Viewer User Journey
tags:
  - scan-story/release-1
  - journey
status: draft
---

# Viewer User Journey

```mermaid
flowchart LR
  A[Open QR or link] --> B[Device capability check]
  B --> C{Supported mode?}
  C -- Full/Standard/Lightweight --> D[Start Experience]
  D --> E[Camera permission]
  E --> F[Guided image scanning]
  F --> G[Recognition result]
  G --> H[Video overlay]
  C -- Unsupported --> Z[Fallback experience]
  E -- denied --> Z
  F -- not found/timeout --> Z
```

## Viewer Requirements

- No account required for public Experiences.
- Loading states must show progress and recovery paths.
- Camera permission is requested only when needed.
- Scanner mode adapts to capability, memory, network, browser, codec, and reduced-motion settings.
- Fallback is always reachable.

## Viewer Success

Viewer reaches either the anchored video overlay or a useful fallback within a bounded time, with no dead-end loading screen.

