# PostgreSQL Local Development

PostgreSQL is the production target for ScanStory V1 because it supports row-level locking, transactional concurrency, durable queue coordination, and production-grade indexing semantics. SQLite remains useful for isolated automated tests and lightweight smoke checks, but it should not be treated as the final concurrency database.

## Modes

Lightweight mode:
- `DATABASE_URL=sqlite:///instance/scanstory-dev.db`
- `SCANSTORY_QUEUE_MODE=fake` or `inline`
- No Redis required

Full integration mode:
- `DATABASE_URL=postgresql+psycopg://username:password@localhost:5432/scanstory_dev`
- `REDIS_URL=redis://127.0.0.1:6379/0`
- `SCANSTORY_QUEUE_MODE=rq`
- `RQ_QUEUE_NAME=scanstory-processing`
- `RQ_DEFAULT_TIMEOUT=600`

Production mode:
- Provisioned only during deployment.
- Never use production credentials in local scripts, docs, screenshots, or logs.

## Driver

Use `psycopg[binary]` with SQLAlchemy's `postgresql+psycopg://` URL form. The binary extra keeps Windows local setup simple and avoids requiring a compiler.

## Safe Setup

Create a disposable local database outside this repository. Do not use production database names or passwords.

Example PowerShell placeholders:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://username:password@localhost:5432/scanstory_dev"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
$env:SCANSTORY_QUEUE_MODE = "rq"
$env:RQ_QUEUE_NAME = "scanstory-processing"
$env:RQ_DEFAULT_TIMEOUT = "600"
```

Check config:

```powershell
.\scripts\dev\postgres-config-check.ps1
.\scripts\dev\show-queue-config.ps1
.\scripts\dev\check-redis.ps1
```

Apply existing Alembic migrations only:

```powershell
.\scripts\dev\postgres-migrate.ps1 -ConfirmApply
.\scripts\dev\postgres-status.ps1
```

Seed disposable development test users only when local dev-test entitlement is intentionally enabled:

```powershell
$env:FLASK_ENV = "development"
$env:SCANSTORY_DEV_TESTING = "1"
.\scripts\dev\postgres-seed-test-users.ps1 -ConfirmApply
```

Start Flask and worker as separate processes:

```powershell
python -m flask --app app run
.\scripts\dev\start-rq-worker.ps1
```

Validate readiness:

```powershell
curl http://127.0.0.1:5000/ready
```

## Prohibited Commands

Do not run:
- `flask db migrate`
- `db.create_all()`
- `git clean`
- database drop, truncate, or schema reset commands
- any command against a shared or production database

## Troubleshooting

If Redis readiness fails, confirm `REDIS_URL` points to a local Redis process and that `SCANSTORY_QUEUE_MODE=rq` is set only when the worker is expected to run.

If `flask db current` does not match `flask db heads`, run only `flask db upgrade` against a disposable development database.

If PostgreSQL connection fails, check host, port, database name, and local firewall. Do not print password-bearing URLs in logs or tickets.
