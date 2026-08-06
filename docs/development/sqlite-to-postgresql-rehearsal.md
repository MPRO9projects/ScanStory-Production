# SQLite To PostgreSQL Rehearsal

`scripts/migration/sqlite_to_postgresql_rehearsal.py` is a dry-run-first development rehearsal tool. It is not a production migration executor.

The tool:
- reads from an explicit `sqlite:///...` source URL;
- writes to an explicit `postgresql://...` or `postgresql+psycopg://...` destination URL only when `--apply` is present;
- never modifies the SQLite source;
- never creates schema;
- never runs `db.create_all()`;
- never generates migrations;
- verifies the PostgreSQL destination is already at the current Alembic head;
- refuses non-empty destinations unless `--allow-non-empty-destination` is explicitly supplied;
- imports tables in SQLAlchemy dependency order using `db.metadata.sorted_tables`;
- preserves integer IDs by inserting explicit values;
- resets PostgreSQL sequences after preserved-ID imports;
- reports row counts and skipped/missing tables;
- does not copy media files.

Dry-run example:

```powershell
python scripts\migration\sqlite_to_postgresql_rehearsal.py `
  --sqlite-source sqlite:///C:/path/to/source.db `
  --postgres-dest postgresql+psycopg://username:password@localhost:5432/scanstory_rehearsal `
  --report rehearsal-report.json
```

Apply example for a disposable destination:

```powershell
python scripts\migration\sqlite_to_postgresql_rehearsal.py `
  --sqlite-source sqlite:///C:/path/to/source.db `
  --postgres-dest postgresql+psycopg://username:password@localhost:5432/scanstory_rehearsal `
  --apply `
  --report rehearsal-report.json
```

Do not use `scanstory_local.db`. Do not use production credentials. Do not point the destination at a production database.

## Domain Coverage

The rehearsal follows model/table dependency order and therefore accounts for:
- organizations and workspaces;
- users and admins;
- subscription plans and trial details;
- payment orders, reservations, and webhook records;
- projects and project pairs;
- experiences, versions, triggers, assets, and recognition artifacts;
- scan logs and scan events;
- processing jobs/events;
- upload sessions;
- configuration records;
- login/admin activity records;
- OTP records when present.

## Policy Review Tables

Some tables are security-sensitive or operationally optional:
- `otp_codes`: migrate only if testing authentication-state continuity is explicitly required.
- `user_login_activity`: useful for audit continuity, optional for functional rehearsal.
- `admin_activity`: useful for audit continuity, optional for functional rehearsal.

Media files are not copied. The report only preserves database references so missing media can be reviewed separately.

If a rehearsal fails during `--apply`, treat the destination as disposable unless the report and PostgreSQL transaction state prove a full rollback occurred. The safe default is to drop and recreate only the disposable rehearsal database outside this tool, then rerun migrations and rehearsal.
