---
title: Billing And Entitlement Review
tags: [scan-story/release-1, readiness/billing]
status: draft
---

# Billing And Entitlement Review

## Current Code

Current billing is user-owned: `User.subscription_id`, `subscription_status`, `subscribed_project_limit`, `subscribed_scan_limit`, `projects_used`, `scans_used`. `SubscriptionPlan` stores plan price/duration/project/scan limits. `PaymentOrder` records Razorpay order/payment/signature and plan limits at purchase.

## Target Gap

Release 1 wants Workspace billing, entitlement keys, usage records, contract overrides, Experience Views, and non-billable internal recognition attempts.

## Minimum Release 1 Billing Changes

- Workspace billing account or compatibility wrapper.
- Entitlement table or structured config.
- Usage records separate from recognition attempts.
- Policy for fallback launch counting.
- Overage/grace rules.
- Idempotent payment state protection.

## Deferred Billing

Tax, invoicing automation, purchase orders, marketplace billing, multi-currency expansion, and developer API billing.

Billing readiness score: **56/100**.

