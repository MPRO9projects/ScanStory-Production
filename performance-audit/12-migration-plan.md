# Migration Plan

## Stage 1 - Baseline

- Capture current Lighthouse and server metrics.
- Document production command, reverse proxy, DNS, SSL, env vars, DB, media directories.
- Take DB and media backups.
- Rollback: keep current server unchanged.

## Stage 2 - Code Optimization

- Fix approved Priority 0 and Priority 1 items only.
- Run smoke tests and performance tests.
- Rollback: revert approved optimization commits/deployment.

## Stage 3 - AWS Staging

- Create staging app, DB, S3 bucket, CloudFront, logs.
- Restore sanitized DB/media subset.
- Test uploads, scanner, payments in test mode, SMTP, reCAPTCHA.
- Rollback: destroy staging or stop routing traffic.

## Stage 4 - Load And Reliability

- Run low-to-moderate load tests.
- Test restart, worker failure, DB reconnect, backup restore, cache invalidation.
- Rollback: do not promote staging.

## Stage 5 - Production Cutover

- Lower DNS TTL.
- Final backup and media sync.
- Deploy app.
- Switch DNS.
- Verify core flows.
- Rollback: restore DNS to old server within TTL window.

## Stage 6 - Post-Migration

- Watch CPU, RAM, latency, DB connections, errors, bandwidth, Core Web Vitals.
- Resize down or up after real usage.

