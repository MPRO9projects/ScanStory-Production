---
title: API And Module Boundaries
tags:
  - scan-story/release-1
  - architecture
status: draft
---

# API And Module Boundaries

Release 1 can remain a modular Flask app. It does not need microservices. It does need clear service interfaces.

```mermaid
flowchart TD
  UI[Creator Dashboard] --> API[Web/API Layer]
  Viewer[Public Viewer] --> API
  API --> Identity[Identity]
  API --> Workspaces[Workspaces]
  API --> Billing[Billing and Entitlements]
  API --> Experiences[Experiences]
  Experiences --> Triggers[Triggers]
  Triggers --> Assets[Assets]
  Assets --> Media[Media Processing]
  Triggers --> CV[Computer Vision]
  Experiences --> Publishing[Publishing]
  Publishing --> QR[QR and Links]
  Viewer --> Scanner[Scanner Runtime]
  Scanner --> CV
  API --> Analytics[Analytics]
  API --> Admin[Admin]
```

## Future Add-On Isolation

```mermaid
flowchart LR
  Core[Release 1 Image Video Core] --> AddonAPI[Versioned Add-On Interfaces]
  AddonAPI --> ThreeD[Future 3D]
  AddonAPI --> WebXR[Future WebXR]
  AddonAPI --> Native[Future Native SDK]
  AddonAPI --> Marketplace[Future Marketplace]
```

The scanner, recognition, publishing, entitlement, and analytics contracts must be versioned so future add-ons do not rewrite the core.

