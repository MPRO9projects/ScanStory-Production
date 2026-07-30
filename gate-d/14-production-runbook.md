# Production Runbook

## Before Migration

1. Confirm approved commit.
2. Confirm no uncommitted changes.
3. Confirm no remote/push requirement for local rehearsal.
4. Take and verify a database backup.
5. Restore backup into rehearsal.
6. Run source-data profile.
7. Prepare ownership mapping file.
8. Resolve admin-owned and unknown-owner project exceptions.
9. Run dry-run.
10. Review exception report.
11. Confirm maintenance strategy.
12. Confirm rollback decision point.
13. Confirm logs and disk capacity.

## Apply

1. Set explicit database URL.
2. Set migration environment.
3. Run migration apply.
4. Monitor logs and checkpoint counts.
5. Stop on blocking conditions.

## Verify

1. Reconcile user/workspace counts.
2. Reconcile project/experience mappings.
3. Reconcile pair/trigger mappings.
4. Verify legacy QR routes.
5. Verify scanner contract.
6. Verify login.
7. Verify projects.
8. Verify payments.
9. Verify media paths.

## After Migration

1. Keep compatibility mode enabled.
2. Do not remove legacy tables.
3. Monitor errors.
4. Retain backup.
5. Approve next gate only after verification.
