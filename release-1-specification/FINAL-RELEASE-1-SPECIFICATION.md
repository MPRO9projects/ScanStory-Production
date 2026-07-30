---
title: Final Release 1 Specification
tags:
  - scan-story/release-1
  - specification
status: draft
---

# Final Release 1 Specification

This pack defines Release 1 as a focused SaaS product for **image-triggered video Experiences**. The product stays narrow on purpose: creator uploads reference images and videos, tests recognition quality, publishes a permanent QR/link, and viewers scan in-browser or use fallback.

## Included Documents

1. Product definition
2. Release scope
3. Personas
4. Creator journey
5. Viewer journey
6. Admin/support journey
7. Workspace and organization model
8. Experience/trigger content model
9. Experience lifecycle
10. Processing and publishing model
11. Recognition and quality strategy
12. Mobile and device strategy
13. Fallback strategy
14. QR/public link model
15. Bulk and enterprise foundation
16. SaaS plans and entitlements
17. Education/agency/enterprise models
18. Managed service and custom solutions
19. Billing and metering
20. Security/privacy
21. Performance budget
22. Scalability/reliability
23. Analytics/observability
24. Data model proposal
25. API/module boundaries
26. Migration model
27. Acceptance criteria
28. Implementation phases
29. Open decisions

## Architecture Summary

Keep the app modular. Do not jump to microservices. Split identity, workspaces, billing, entitlements, Experiences, triggers, assets, media processing, computer vision, publishing, QR/links, public viewer, scanner, analytics, notifications, admin, managed services, and integrations behind clear interfaces.

## Release 1 Guardrail

If a feature does not directly support image-triggered video creation, publishing, viewing, billing, support, reliability, or migration, it waits.

Specification ready for review.

