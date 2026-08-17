# Deployment Runbook

This sequence is intentionally explicit. Run one controlled environment at a
time. Do not run commands against real/shared databases from a local shell.

## Ordered Deployment Sequence

1. Freeze the release commit and record the full SHA.
2. Confirm `git status --short` is clean.
3. Capture relational database backup.
4. Capture upload/media backup.
5. Confirm backup integrity and restore location.
6. Verify environment variables without printing values.
7. Put release files in place.
8. Install dependencies from the approved requirements file.
9. Create or verify the artifact/version record.
10. Verify writable paths for images, videos, feature artifacts, QR assets, and
    logs.
11. Run `flask db heads`.
12. Run `flask db history`.
13. Run `flask db current`.
14. Run migration duplicate-preflight checks.
15. Review offline SQL where supported.
16. Run migration only after explicit approval.
17. Restart the application process.
17a. **Start (or restart) the RQ worker service** — `python rq_worker.py` with the
    same `REDIS_URL` / `SCANSTORY_QUEUE_MODE=rq` / `RQ_QUEUE_NAME` as the web
    process, under a supervisor that restarts it automatically. Uploads are
    queued by the web process and executed only by this worker.
18. Verify `GET /healthz`.
19. Verify `GET /ready` — it must report `checks.workers == "ok"` and
    `usable_worker_count >= 1`. A 503 with `workers: "unavailable"` means the
    worker is not running or its heartbeat is stale; do not release traffic.
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

The first production traffic release must not happen before health, readiness,
login, admin, upload, scanner, media, suspension, and Razorpay test-mode staging
evidence are recorded for the exact release commit.

## Required Long-Running Processes

| Process | Command | Required in production |
|---|---|---|
| Web application | behind gunicorn/waitress (never `flask run`) | Yes |
| RQ processing worker | `python rq_worker.py` | **Yes** — uploads queue but never process without it |

Both must be supervised with automatic restart, and both must read the same
`REDIS_URL`, `SCANSTORY_QUEUE_MODE=rq` and `RQ_QUEUE_NAME`.

## Scheduled Maintenance Commands

These reconciliation/recovery commands are dry-run by default and must be
**scheduled**, not run only when someone notices a problem. Suggested cadence;
tune with real volume. Review the output of every run — several exit non-zero
when a human is required.

| Command | Cadence | Non-zero exit means |
|---|---|---|
| `flask reconcile-refunds` | hourly | a refund is stuck or an out-of-band provider refund is unresolved — investigate immediately (money path) |
| `flask reconcile-storage --json` | daily | ambiguous media ownership or a hard reconciliation error. Never deletes files; orphans are reported only |
| `flask expire-ownership-transfers --apply` | daily | — (closes handover offers past their deadline; ownership is never changed) |
| `flask expire-stale-reservations --apply` | every 15 min | — |
| `flask recover-processing-jobs --apply` | every 15 min | — |
| `flask reconcile-capacity-reservations` | daily | counter drift needing repair |
| `flask cleanup-upload-sessions --apply` | daily | — |

Run each one first without `--apply` in a new environment and record the output
before enabling the scheduled `--apply` form.

## Deployment Stop Conditions

- Database migration preflight fails.
- Backups cannot be verified.
- `/healthz` or `/ready` fails after restart.
- `/ready` reports zero usable RQ workers (`checks.workers: "unavailable"`).
- Login, upload, scanner, or payment activation smoke test fails.
- Authorization regression is detected.
- Logs expose secrets or private data.
- Error rate exceeds the agreed threshold.

Escalate to `[Rollback Authority Role]` when any stop condition is met.
