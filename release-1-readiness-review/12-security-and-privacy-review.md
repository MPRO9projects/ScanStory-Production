---
title: Security And Privacy Review
tags: [scan-story/release-1, readiness/security]
status: draft
---

# Security And Privacy Review

## Findings

| Finding | Severity | Notes |
|---|---|---|
| Workspace/tenant authorization model missing | Critical | Required before multi-tenant Release 1. |
| Public scanner sets `user_id` from QR query into session | High | Compatibility-sensitive billing/counting risk. |
| CSRF coverage not confirmed across POST routes | High | Spec requires CSRF. |
| OTP abuse controls incomplete | High | OTP exists; rate-limit policy must be explicit. |
| Upload validation is size-focused in current upload path | High | Signature/MIME/probing/malware integration needed. |
| In-process processing handles uploaded media | Medium | Needs secure worker isolation. |
| Public/private Experience model open | High | Access control blocker. |
| Debug/secret production policy not fully evidenced | Medium | `.env` ignored; runtime config unknown. |
| Privacy-safe analytics direction is good | Low | Must preserve no-camera-frame collection. |

Security readiness score: **48/100**.

