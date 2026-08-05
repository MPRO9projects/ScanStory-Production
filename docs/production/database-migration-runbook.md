# Database Migration Runbook

Known migration chain:

```text
3914ece79b88 -> bc5642a86981 -> 54a108a17fa7 -> ebeab1cf4ec9
```

`ebeab1cf4ec9` (razorpay webhook events) is a pure ADD migration: it only
creates the new `razorpay_webhook_events` table and its indexes, including a
UNIQUE index on `idempotency_key` (the DB-backed replay-protection gate for
`POST /webhooks/razorpay` — not an in-memory check). It does not alter any
existing payment/capacity table, so its `downgrade()` only drops what it
itself created and carries no destructive risk to prior data.

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
- After upgrading past `ebeab1cf4ec9`, confirm `RAZORPAY_WEBHOOK_SECRET` is
  configured before relying on webhook reconciliation — the table existing
  does not mean the webhook route is usable; the route itself fails closed
  (rejects, processes nothing) whenever that secret is absent.

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

Read-only Razorpay webhook inspection CLIs (added by `ebeab1cf4ec9`; none of
these mutate `razorpay_webhook_events`, `payment_orders`, or any other table):

```powershell
python -m flask --app app webhook-events-status
python -m flask --app app webhook-events-status --limit 50
python -m flask --app app reconcile-order-webhooks <order_id>
python -m flask --app app webhook-replay-report
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
