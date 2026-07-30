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

## Revision 1 Legacy QR Compatibility

Decision status: Approved Release 1 rule.

Existing QR codes and existing `/scanner/<project_id>` links must continue working throughout Release 1 migration. Existing customers must not be required to reprint QR codes.

Compatibility strategy:

```text
Existing QR -> legacy route -> compatibility resolver -> current Published Experience Version -> scanner
```

## Compatibility Resolver Rules

- Legacy QR route: `/scanner/<project_id>` remains available until explicit retirement approval after Release 1.
- New permanent Experience route: `/e/{public_experience_key}` uses a generated public key and never exposes internal IDs.
- Route alias behavior: legacy route may directly render the legacy scanner or resolve to the current published Experience; it must preserve user-visible behavior.
- Legacy Project ID lookup: supported for existing Projects and QR files.
- Migrated Experience lookup: uses public key first, then compatibility mapping for legacy IDs.
- Redirect versus direct compatibility: redirects are allowed only if scanner behavior, scan counting, and media access remain identical for legacy viewers.
- Paused/archived behavior: permanent link remains valid and shows controlled unavailable/fallback policy; it does not 404 for a valid archived key unless deletion policy requires it.
- Invalid link behavior: viewer-safe message, fallback/help route where possible, diagnostic event.
- QR regeneration: regenerates assets for the same stable key unless explicitly creating a new optional Trigger QR.
- Version independence: QR points to the Experience public key, not a mutable draft or asset path.
