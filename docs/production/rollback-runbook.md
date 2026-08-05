# Rollback Runbook

Every deployment must identify one rollback authority before release:
`[Rollback Authority Role]`.

## Rollback Triggers

- Application fails to start.
- `/healthz` failure.
- `/ready` failure.
- Login unavailable.
- Scanner unavailable.
- Payment activation broken.
- Migration failure.
- Data corruption.
- Media inaccessible.
- Authorization regression.
- Secret leakage.
- Unacceptable error rate.

## Application-Only Rollback

Use when code deployment fails but database and media state are healthy.

1. Stop traffic or remove the bad instance from rotation.
2. Restore the previous application artifact.
3. Keep the database at the current version unless rollback authority approves
   database restore.
4. Restart application.
5. Verify `/healthz`, `/ready`, login, upload, scanner, media, and payment smoke.
6. Monitor error rate and logs.

## Migration/Data Rollback

Use when schema or data changes are unsafe.

1. Stop writes if possible.
2. Preserve current failed state for forensic review.
3. Restore the verified database backup from the consistency point.
4. Deploy application version compatible with restored database.
5. Verify Alembic current version.
6. Run smoke and payment checks.

Do not run `flask db downgrade base` in production.

## Media/Storage Rollback

1. Stop writes to affected storage path.
2. Restore images, videos, feature artifacts, and QR assets from the same
   consistency point as the database where possible.
3. Verify checksums or sampled integrity.
4. Test project preview, scanner, Range media, and suspended project blocking.

## Credential Rotation Response

If logs or incidents expose secrets:

1. Revoke/rotate affected secret in the approved secret manager.
2. Restart affected application processes.
3. Invalidate sessions if session-signing material was exposed.
4. Review logs for misuse.
5. Create incident report.
