---
title: SaaS Plans And Entitlements
tags:
  - scan-story/release-1
  - billing
status: draft
---

# SaaS Plans And Entitlements

Editions: Creator, Professional, Business, Education, Enterprise, Studio. Developer is future.

Prices are configurable and never hardcoded into business logic.

Decision status: Approved Release 1 rule for ownership and metering; configurable policy for commercial values.

Billing Account belongs to Workspace. Current User subscription fields remain temporarily supported during migration and are wrapped by compatibility logic until Workspace billing is validated.

## Entitlement Keys

max_active_experiences, max_triggers_per_experience, monthly_experience_views, storage_bytes, media_processing_minutes, workspace_members, analytics_retention_days, custom_branding, remove_platform_branding, custom_domain, bulk_upload, approval_workflow, audit_logs, api_access, priority_processing, support_level, education_features, enterprise_security, managed_service_access, future_3d, future_webxr.

## Enforcement

Entitlements are checked before creation, upload, processing, publishing, analytics retention, branding changes, and domain activation. Live Experiences should not abruptly stop at allowance exceed unless the plan policy explicitly says so.

Grace and overage behavior is configurable. Recommended default: soft-limit live published Experiences with owner notification, while blocking new publishes or new heavy processing when policy limits are exceeded.
