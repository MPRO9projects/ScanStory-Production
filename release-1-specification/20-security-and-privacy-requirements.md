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

## Revision 1 Security Gates

Decision status: Approved Release 1 rule.

| Control | Gate |
|---|---|
| CSRF protection | Gate A |
| Secure session configuration | Gate A |
| Authorization checks | before new data model |
| Workspace tenant isolation | before new data model |
| Admin authorization | Gate A |
| Login rate limiting | before public staging |
| OTP rate limiting | before public staging |
| Password-reset rate limiting | before public staging |
| Payment idempotency | before public staging |
| Upload MIME validation | before public staging |
| Extension validation | before public staging |
| File-signature validation | before public staging |
| Safe image decoding | before public staging |
| Safe video probing | before public staging |
| Configurable upload size | before public staging |
| Secret management | before public staging |
| Debug disabled in production | before public staging |
| Viewer-safe errors | Gate A |
| Private Experience authorization | before production |
| Audit events | before public staging |
| Data retention | before production |
| Privacy-safe scanner analytics | before public staging |
| No storage of camera frames by default | Gate A |
