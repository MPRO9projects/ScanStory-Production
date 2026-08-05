# Backup and Restore Runbook

Do not claim a backup exists unless it has been verified.

## Backup Scope

- Relational database.
- Uploaded marker images.
- Uploaded videos.
- Generated feature artifacts.
- QR assets.
- Configuration secrets through the approved secret manager.
- Alembic migration version information.

## Frequency and Retention

- Database: at least daily plus pre-deployment backup.
- Media/artifacts: at least daily plus pre-deployment backup.
- Retention: keep short-term daily backups and longer-term weekly/monthly
  backups according to business policy.
- Keep encrypted off-host copies.

## Consistency Point

Database and filesystem backups should be captured from a known consistency
point. If the app remains writable, record the exact capture window and expect
some reconciliation work during restore.

## Integrity Verification

- Verify backup command exit code.
- Verify backup file exists and is non-empty.
- Record checksum.
- Sample database restore into an isolated environment.
- Sample media restore and decode representative image/video files.
- Verify feature artifacts and QR assets are present.

## Restore Rehearsal

At least once before production launch:

1. Restore database into isolated environment.
2. Restore media/artifact directories.
3. Start application against restored copy.
4. Verify Alembic current version.
5. Run health/readiness checks.
6. Test login, Admin, upload, scanner, media, suspension, and payment test-mode
   activation.

## Restore Validation Checklist

- `/healthz` returns 200.
- `/ready` returns 200.
- User login works.
- Admin/Super Admin access works.
- Existing projects load.
- Existing media serves and Range requests work.
- Scanner loads for restored projects.
- Suspended projects remain blocked.
- Payment records are internally consistent.
- Logs do not show missing files or schema errors.
