# Staging Certification

Run this certification on an HTTPS staging environment that uses staging-safe
credentials and a staging database copy. Do not use production secrets or a
shared production database.

## Entry Criteria

- Staging release commit is recorded.
- Staging database is disposable or restored from an approved masked copy.
- Staging storage paths are writable and not shared with production.
- Razorpay test-mode credentials are configured.
- SMTP uses staging-safe recipients or provider sandbox mode.
- `/healthz` and `/ready` return expected contracts.
- Logs are accessible to the certification operator.

## Certification Matrix

| Area | Check | Expected Result |
| --- | --- | --- |
| Liveness | `GET /healthz` | HTTP 200, JSON liveness, `Cache-Control: no-store`. |
| Readiness | `GET /ready` | HTTP 200 when DB is ready, 503 when DB is unavailable, no secret text. |
| Auth | User login/logout | Works without leaking whether unrelated accounts exist. |
| Admin | Admin and Super Admin login | Authorized roles reach expected pages only. |
| Upload | Project image/video upload | Valid media creates project; invalid media is rejected before rows/quota. |
| Scanner | Public scanner load | Camera/scanner page loads and can reach detection endpoints. |
| Media | Public image/video/QR | Serves authorized media; Range request returns 206 where applicable. |
| Suspension | Suspended project | Scanner and media routes are blocked server-side immediately. |
| Cache | Public media | `public, max-age=3600`; note browser cache window after suspension. |
| Cache | Admin media | `private` cache only. |
| Payment | Razorpay test checkout | Order, signature validation, activation, and capacity reservation pass. |
| Logging | Secret hygiene | No secrets, raw signatures, credentials, cookies, or full payment payloads. |

## Staging Sign-Off

Record:

- Release commit.
- Database migration head/current output.
- Health/readiness baseline.
- Smoke-test results.
- Razorpay test transaction identifiers without secrets.
- Open defects and accepted risks.
- Rollback authority approval.

Do not certify production if any P0 or data-integrity issue remains unresolved.
