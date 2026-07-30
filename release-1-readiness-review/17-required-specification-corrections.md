---
title: Required Specification Corrections
tags: [scan-story/release-1, readiness/spec]
status: draft
---

# Required Specification Corrections

| File | Section | Severity | Required Correction |
|---|---|---|---|
| `07-workspace-and-organization-model.md` | Billing ownership | High | State Workspace owns billing; legacy User subscriptions wrap during migration. |
| `14-qr-and-public-link-model.md` | Compatibility | High | Add rule preserving `/scanner/<project_id>` and existing QR files. |
| `10-processing-and-publishing-model.md` | QR timing | High | Distinguish legacy upload QR from new publication activation. |
| `19-billing-and-usage-metering.md` | Billing unit | High | Say fallback launches count as Experience Views; recognition attempts do not. |
| `24-data-model-proposal.md` | Contract overrides | Medium | Add BillingAccount, Entitlement, ContractOverride, UsageRecord. |
| `26-migration-from-current-model.md` | Rollback | High | Add additive/dual-read/rollback rules. |
| `12-mobile-and-device-strategy.md` | Failure states | Medium | Add failure matrix with timeout/retry/message/fallback/diagnostic/event. |
| `27-release-acceptance-criteria.md` | Measurability | Medium | Convert broad bullets into testable criteria. |
| `29-open-decisions.md` | Missing decisions | High | Add workspace billing, scanner versioning, migration rollback, deletion/archive. |

