# Database Migration Runbook

Known migration chain:

```text
3914ece79b88 -> bc5642a86981 -> 54a108a17fa7
```

## Rules

- Use a staging copy first.
- Back up the database before migration.
- Never blindly run a baseline upgrade against an already-created production
  schema.
- Existing production database may be stamped at the baseline only after schema
  verification proves the schema already matches that baseline.
- Run duplicate Razorpay ID preflight before unique constraints.
- Upgrade one controlled environment at a time.
- Verify application behavior after migration.

## Required Commands

Run from the deployed environment, using that environment's approved execution
path:

```powershell
python -m flask --app app db heads
python -m flask --app app db history
python -m flask --app app db current
```

Current application-level maintenance CLIs that are relevant to the integrated
quota/payment/capacity state:

```powershell
python -m flask --app app reconcile-quota-counters
python -m flask --app app reconcile-quota-counters --repair
python -m flask --app app capacity-status
python -m flask --app app expire-stale-reservations
python -m flask --app app expire-stale-reservations --apply
python -m flask --app app reconcile-capacity-reservations
python -m flask --app app reconcile-capacity-reservations --apply
```

Dry-run/report modes must be captured before any `--repair` or `--apply`.
The verification scripts in this package do not run `flask db upgrade`,
`--repair`, or `--apply`.

Where supported, generate offline SQL for review before applying migration:

```powershell
python -m flask --app app db upgrade --sql
```

Do not run upgrade until the SQL review, duplicate preflight, and backup checks
are complete.

## Duplicate Razorpay ID Preflight

Before applying unique constraints, inspect duplicate groups for Razorpay order
and payment identifiers. The report must include group count and affected row
count, but must not print customer emails, private notes, raw signatures, or
credentials.

If duplicates conflict, stop and decide whether to consolidate manually or
restore/replay from backup. Do not silently delete payment history.

## Rollback Decision Criteria

Rollback or restore backup when:

- Migration fails partway.
- Application cannot start after migration.
- `/ready` fails due to schema errors.
- Payment activation or quota reservation breaks.
- Data corruption is detected.
- Authorization checks regress.

## Downgrade Warnings

- Baseline downgrade may be destructive.
- Never run `flask db downgrade base` in production.
- Production rollback may require restoring the database backup rather than
  relying on Alembic downgrade.
