---
title: Migration From Current Model
tags:
  - scan-story/release-1
  - migration
status: draft
---

# Migration From Current Model

```mermaid
flowchart LR
  User --> WorkspaceMember[Workspace Member]
  Project --> Experience
  ProjectPair --> Trigger
  ImageFile[Image file] --> ReferenceAsset[Reference Asset]
  VideoFile[Video file] --> ContentAsset[Content Asset]
  NPZ[.npz feature] --> RecognitionArtifact[Recognition Artifact]
  ProjectQR[Project QR] --> PublicLink[Permanent Experience Link and QR]
  ScanLog --> Session[Experience Session]
  ScanLog --> Event[Recognition Event]
```

## Migration Rules

- Existing URLs and QR codes continue working.
- Existing projects are not recreated unnecessarily.
- Published behavior remains compatible.
- New versioned models can wrap current records first.
- Data movement should be reversible until final cutover.
- Migration includes validation reports and rollback plan.

