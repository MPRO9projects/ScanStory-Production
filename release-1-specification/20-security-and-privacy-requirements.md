---
title: Security And Privacy Requirements
tags:
  - scan-story/release-1
  - security
status: draft
---

# Security And Privacy Requirements

## Required Controls

- CSRF protection.
- Secure sessions and cookie settings.
- Rate limiting for auth, OTP, uploads, scanner endpoints, and public links.
- OTP abuse protection.
- Upload validation by extension, MIME, signature, size, dimensions, duration, and codec.
- Tenant isolation for assets, jobs, analytics, and billing.
- Authorization checks on every workspace-scoped action.
- Audit logging for sensitive actions.
- Payment idempotency and webhook validation.
- Secret management with no debug secrets in production.
- Safe errors with no stack traces or private paths.
- Public/private Experience controls and signed URL option.
- Deletion/export foundations.
- Malware scanning integration point.

## Privacy Rules

Do not collect camera images or biometric identifiers for analytics. Scanner diagnostics must be event-based and sanitized.

