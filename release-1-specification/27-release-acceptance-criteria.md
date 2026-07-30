---
title: Release Acceptance Criteria
tags:
  - scan-story/release-1
  - acceptance
status: draft
---

# Release Acceptance Criteria

Release 1 is accepted when a creator can create, test, publish, share, and measure an image-triggered video Experience, and a viewer can scan or fallback reliably from the permanent QR/link.

## Acceptance Areas

- Auth and workspace flows work.
- Entitlements are enforced.
- Image/video trigger creation works.
- Processing jobs are durable and observable.
- Marker quality is reported.
- Preview and testing are available before publish.
- Publication creates stable QR/link.
- Public scanner supports device modes.
- Video overlay works for supported devices.
- Fallback works for unsupported or failed paths.
- Analytics and usage are recorded.
- Admin/support can diagnose common failures.
- Security and upload validation pass.
- Performance budgets are measured.
- Existing published projects remain compatible.

## Revision 1 Measurable Acceptance Gates

Decision status: Approved Release 1 rule.

- Workspace authz tests prove users cannot access another Workspace's private creator/admin data.
- Legacy `/scanner/<project_id>`, `/qr/<filename>`, `/image/<project_id>/<image_id>`, and `/video/<project_id>/<image_id>` compatibility tests pass.
- Versioned scanner contract tests validate schema version, public keys, Trigger identity, corners, tracking points, confidence, artifact version, session ID, diagnostic code, and retry guidance.
- Migration dry run reports row counts, skipped rows, failures, and rollback checkpoints.
- Published Version immutability tests pass.
- Fallback is verified for denied camera, unsupported browser, OpenCV/WASM failure, detection timeout, paused/unavailable Experience, and media failure.
- Billing tests prove one launch creates at most one Experience View in the session window and recognition attempts are not billable.
- Performance gate records measured baseline before enforcing thresholds.
- Security gate covers CSRF, sessions, rate limits, upload validation, tenant isolation, safe errors, and no camera-frame storage by default.
