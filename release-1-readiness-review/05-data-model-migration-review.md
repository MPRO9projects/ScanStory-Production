---
title: Data Model Migration Review
tags: [scan-story/release-1, readiness/migration]
status: draft
---

# Data Model Migration Review

## Feasibility

Migration feasibility: **High risk, feasible with additive migration and compatibility routing**.

## Safety Requirements

- Existing users, password hashes, admins, trials, subscriptions, payment orders, projects, pairs, media, `.npz` files, QR codes, scanner URLs, and scan logs remain valid.
- Migration runs against a database copy first.
- Additive tables/columns precede any replacement.
- Backfill is idempotent.
- Legacy identifiers remain addressable.
- Rollback restores old app behavior without data loss.

## Required Migration Mechanics

| Mechanic | Required |
|---|---|
| Additive tables | Yes |
| Additive columns | Yes |
| Data backfill | Yes |
| Compatibility mapping | Yes |
| Dual-read behavior | Yes |
| Dual-write behavior | Likely during transition |
| Route aliases/redirects | Yes |
| Compatibility views | Useful |
| Legacy identifiers | Required |
| Versioned recognition artifacts | Required |

## Feasibility Score

Migration readiness score: **55/100**. The migration path is plausible, but too many canonical decisions remain open.

