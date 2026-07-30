---
title: QR And Public Link Model
tags:
  - scan-story/release-1
  - publishing
status: draft
---

# QR And Public Link Model

Stable Experience URL:

```text
scanstory.com/e/{public_experience_key}
```

Optional trigger URL:

```text
scanstory.com/e/{public_experience_key}/t/{public_trigger_key}
```

## Rules

- No internal IDs in public URLs.
- Links are permanent.
- QR generation is environment-aware.
- QR assets support PNG and SVG.
- Branded QR and custom domains are entitlement-gated.
- Publication verifies QR/link health.
- Regeneration never breaks existing published links.
- Restricted Experiences may use signed/private URLs.

