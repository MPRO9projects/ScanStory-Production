# Deployment Runbook

This sequence is intentionally explicit. Run one controlled environment at a
time. Do not run commands against real/shared databases from a local shell.

## Ordered Deployment Sequence

1. Freeze the release commit and record the full SHA.
2. Confirm `git status --short` is clean.
3. Create or verify the artifact/version record.
4. Capture relational database backup.
5. Capture upload/media backup.
6. Confirm backup integrity and restore location.
7. Put release files in place.
8. Install dependencies from the approved requirements file.
9. Verify environment variables without printing values.
10. Verify writable paths for images, videos, feature artifacts, QR assets, and
    logs.
11. Run `flask db heads`.
12. Run `flask db history`.
13. Run `flask db current`.
14. Run migration duplicate-preflight checks.
15. Review offline SQL where supported.
16. Run migration only after explicit approval.
17. Restart the application process.
18. Verify `GET /healthz`.
19. Verify `GET /ready`.
20. Run smoke tests.
21. Test user login.
22. Test Admin and Super Admin access.
23. Test project upload.
24. Test scanner load and scanner API contract.
25. Test public media and Range response.
26. Test suspended project blocking.
27. Test Razorpay test-mode order and activation in staging.
28. Verify logs contain no secrets, credentials, emails in payment payloads, raw
    signatures, auth cookies, or private media paths.
29. Release traffic.
30. Monitor health, readiness, error rate, payment activation, scanner latency,
    and storage utilization.

## Deployment Stop Conditions

- Database migration preflight fails.
- Backups cannot be verified.
- `/healthz` or `/ready` fails after restart.
- Login, upload, scanner, or payment activation smoke test fails.
- Authorization regression is detected.
- Logs expose secrets or private data.
- Error rate exceeds the agreed threshold.

Escalate to `[Rollback Authority Role]` when any stop condition is met.
