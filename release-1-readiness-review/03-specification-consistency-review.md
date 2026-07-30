---
title: Specification Consistency Review
tags: [scan-story/release-1, readiness/spec]
status: draft
---

# Specification Consistency Review

## Summary

Most contradictions are terminology and policy gaps rather than direct conflicts. They must still be recorded because they affect data model, migration, QR behavior, billing, and scanner compatibility.

| Conflict | Files | Severity | Canonical Recommendation | Blocker |
|---|---|---|---|---|
| Project vs Experience | `24`, `26`, current code | High | Current `Project` remains legacy compatibility; new public/product term is Experience. | Yes |
| Pair vs Trigger | `08`, `24`, `26`, current code | Medium | `ProjectPair` maps to Trigger; public APIs should use Trigger only after versioned route. | Yes |
| User billing vs Workspace billing | `16`, `19`, current `models.py` | High | Release 1 target billing account belongs to Workspace; legacy User subscription is wrapped during migration. | Yes |
| QR at upload vs QR at publication | `10`, `14`, current upload route | High | Legacy QR can be created at upload; new Experience QR is stable and activated at publication. | Yes |
| Experience View vs Scan vs Recognition Attempt | `19`, `23`, current `ScanLog` | High | Experience View is viewer launch; Recognition Attempt is scanner detection try; ScanLog legacy maps to session/event. | Yes |
| Delete vs archive | `09`, `20`, `27` | Medium | Archive hides from public; deletion is separate privacy/data-retention operation. | Yes |
| Fallback billing | `13`, `19` | Medium | Fallback launch counts as Experience View if it is a viewer launch; recognition attempts are not billable. | Yes |
| Plan names and limits | `16`, `plan-entitlement-matrix.csv`, current `SubscriptionPlan` | Medium | Plan names are display/config; entitlement keys are canonical enforcement. | Yes |
| Private Experiences | `14`, `20`, `29` | High | Decide before implementation; public Release 1 can support public only or signed/private with explicit model. | Yes |

Consistency score: **72/100**.

