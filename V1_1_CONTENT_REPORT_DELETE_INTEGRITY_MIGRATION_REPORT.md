# V1.1 — ContentReport survives project delete (database-integrity fix)

One narrowly-scoped production database-integrity fix: moderation reports must
outlive the project they were filed against. Nothing else was changed.

---

## 1. Starting integration HEAD

`0ce353db8cfc7a4e4503ec9f26a4b2ab7fd803b0`

Authoritative integration HEAD that this worktree was fast-forward-synced to.
Re-verified at the start of this lane with `git rev-parse HEAD` and
`git status --short` (clean) on branch `agent/v1.1-production-ops`.

## 2. Starting branch HEAD

`0ce353db8cfc7a4e4503ec9f26a4b2ab7fd803b0` on `agent/v1.1-production-ops` —
identical to the integration HEAD, working tree clean, nothing stashed.

## 3. Ending HEAD

The fourth and final commit, `Document the content-report delete-integrity fix`
— the commit that adds this file. A commit cannot contain its own hash, so the
exact SHA is reported to the orchestrator alongside this report; it is also
obtainable with `git rev-parse HEAD` on `agent/v1.1-production-ops`.

The three preceding commits are pinned by full hash in section 4. Nothing was
pushed and integration was not merged.

## 4. Commits

Four narrow commits: db-fix, admin-fix, tests, docs.

| # | Hash | Subject |
|---|---|---|
| 1 | `e73f6e34cdb44ff50a29cd6a8637f18bdf947d87` | Keep content reports when a project is hard-deleted |
| 2 | `d302ca33dc467e32e347b972ec7b8372ba117672` | Render and guard moderation actions on detached reports |
| 3 | `7aa0dd2adff2bdbd61f8429f190be1c9c66e5365` | Cover content-report retention across project deletion |
| 4 | *(this commit)* | Document the content-report delete-integrity fix |

1. **`e73f6e3`** — `models.py` + migration `e9b4d7a2c815`. The schema and ORM
   change: nullable, `ON DELETE SET NULL`, delete-orphan cascade removed.
2. **`d302ca3`** — `app.py` + `templates/admin/moderation.html`. Detached-report
   rendering and the `PROJECT_UNAVAILABLE` guard.
3. **`7aa0dd2`** — the two new test files, plus the one un-pinned head assertion
   in `test_ownership_history_delete_migration.py`.
4. **this commit** — this report.

## 5. Files changed

| File | Change |
|---|---|
| `models.py` | `ContentReport.project_id` → nullable + `ondelete="SET NULL"`; delete-orphan cascade removed from the `Project` backref. |
| `migrations/versions/e9b4d7a2c815_content_report_survives_project_delete.py` | **New.** The schema migration. |
| `app.py` | `_content_report_payload` gains `project_deleted`; `admin_review_content_report` refuses `PROJECT_SUSPENDED` on a detached report; audit line reads `project deleted` instead of `project None`. |
| `templates/admin/moderation.html` | Queue + review modal render the detached state; no project URL is built from a missing id; the suspend option is disabled for a detached report. |
| `tests/migrations/test_content_report_delete_migration.py` | **New.** 16 migration tests. |
| `tests/integration/test_content_report_survives_project_delete.py` | **New.** 22 application tests. |
| `tests/migrations/test_ownership_history_delete_migration.py` | One assertion un-pinned: it asserted `c1a7f3d95e24` *was* the Alembic head; it now asserts the history stays linear (exactly one head) and that `c1a7f3d95e24` is an ancestor of it. |

Nothing else was touched. No scanner file, no upload architecture, no
pricing/payment/subscription code, no unrelated schema or cascade cleanup, no
`Query.get` deprecation fixes.

## 6. Exact old ContentReport FK

Created inline by `a1c3e5b7d9f2` (Wave 5's moderation work):

```python
sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
```

Reflected before the upgrade:

```
{'name': None, 'constrained_columns': ['project_id'],
 'referred_table': 'projects', 'referred_columns': ['id'], 'options': {}}
```

**Unnamed** (server-assigned, so the real name differs per backend —
PostgreSQL calls it `content_reports_project_id_fkey`), `NOT NULL`, and **no**
`ON DELETE` clause, i.e. PostgreSQL's default `NO ACTION`.

**The destructive mechanism was NOT the database constraint.** This was proven
by reading the actual constraint definition rather than assumed: at the DB level
a plain `NO ACTION` FK would have *blocked* the delete, not cascaded it. The
destruction came from the ORM:

```python
project = db.relationship("Project",
    backref=db.backref("content_reports", lazy=True, cascade="all, delete-orphan"))
```

`cascade="all, delete-orphan"` made SQLAlchemy delete every `ContentReport` row
before deleting the project. So the fix had to address **both** layers — a
database `SET NULL` rule alone would have been silently overridden by the ORM
cascade.

## 7. Exact new ContentReport FK

```
{'name': 'fk_content_reports_project_id_projects',
 'constrained_columns': ['project_id'],
 'referred_table': 'projects', 'referred_columns': ['id'],
 'options': {'ondelete': 'SET NULL'}}
```

Explicitly named per the repo convention (`fk_<table>_<column>_<referred>`, the
same shape `d4e8b2c6a0f3` introduced for `fk_upload_sessions_project_id_projects`).

ORM side:

```python
project_id = db.Column(
    db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
)
project = db.relationship("Project", backref=db.backref("content_reports", lazy=True))
```

This matches exactly how `UploadSession.project_id` encodes the same decision
(P0-5, `models.py:2293`). No `delete-orphan` cascade was added back — that would
have recreated the destructive behaviour at the ORM layer even with a correct DB
constraint. The default relationship cascade de-associates (nulls the FK) on
parent delete, which agrees with the database rule instead of fighting it.

## 8. Nullable change

`content_reports.project_id`: `nullable=False` → `nullable=True`.

That is the whole reason the row can survive: with `NOT NULL` there is no
representation for "report whose project is gone", so the row had to either
cascade away or block the delete. Reflected before/after in the migration tests
(`nullable is False` at `c1a7f3d95e24`, `nullable is True` at `e9b4d7a2c815`).

No other column's nullability was touched.

## 9. ON DELETE behaviour

`NO ACTION` (default) → `ON DELETE SET NULL`.

Enforced at the **database** level, not in the delete helper, so every path is
covered — including the ORM cascades from `Admin.projects` / `User` and any raw
`DELETE` that never goes through `_delete_project_files_and_rows`. No
application-layer "set project_id to NULL before delete" step was added to
`_delete_project_files_and_rows`; the constraint owns this, which was the point
of the fix. (Contrast `UploadSession`, which needs an explicit helper update
because it has no ORM relationship to nullify it.)

## 10. Existing-data preservation

Non-destructive. No row deleted, no column dropped or retyped, no value reset,
no backfill invented.

Verified: a report pointing at a live project keeps that `project_id`
**unchanged** across the upgrade — only future deletions detach. Proven with
two seeded reports on two projects (`test_upgrade_leaves_existing_project_id_values_untouched`),
with a 9-report/3-project fixture (`test_upgrade_handles_many_projects_each_with_several_reports`),
and on an empty table (`test_upgrade_works_on_an_empty_content_reports_table`).

Every field is preserved through a delete: `reason`, `details`, `status`,
`resolution_action`, `resolution_reason`, `metadata_json`,
`reviewed_by_admin_id`, `reviewed_at`, `created_at`, `reporter_user_id`,
`reporter_email`, `reporter_session_hash`, `reporter_ip_hash`.

All nine indexes `a1c3e5b7d9f2` created survive the rebuild — asserted
explicitly, because on SQLite the table is physically recreated and this is a
real risk rather than a formality:
`ix_content_reports_project_id`, `_reporter_user_id`, `_reporter_session_hash`,
`_reporter_ip_hash`, `_reason`, `_status`, `_reviewed_by_admin_id`,
`_project_status`, `_created_at`.

## 11. Hard-delete behaviour

`admin_delete_project` → `_delete_project_files_and_rows` semantics are
**unchanged**: the lifecycle guard (`project_deletion_block_reason`) still runs
first, media/features/QR are still unlinked, storage accounting is still
released only after a successful physical delete, `UploadSession` references are
still cleared, ownership history is still detached, pairs and the project row are
still deleted, `AdminActivity` is still written.

The only behavioural difference: reports filed against the project are no longer
destroyed. Verified — after a hard delete the project row is gone, its
`ProjectPair` rows are gone, the image and video files are gone from disk, and
the `ContentReport` row is still present.

Reports do **not** block deletion (`project_deletion_block_reason` was not
touched, and it only considers active ownership transfers/claims).

## 12. Detached-report behaviour

A detached report is a normal, queryable row with `project_id IS NULL`. It
retains its own identity and every moderation fact. It remains listable in the
queue, readable in detail, and reviewable. Nothing about the reporter is lost,
and nothing about the project is invented.

Deleting one project detaches only that project's reports — reports on other
projects keep their `project_id`, status and metadata untouched.

## 13. Admin report rendering

`_content_report_payload` was already `None`-safe for `project_name`,
`project_owner_type`, `project_owner_user_id`, `project_is_active` and
`project_is_publicly_live` (the prior lane made it so). One field was added:

```python
"project_deleted": report.project is None,
```

so the page can branch on the state explicitly instead of inferring it from a
missing field. A detached report returns `project_id: null`,
`project_name: null`, `project_deleted: true`, and `null` for every
project-derived fact — safe semantics, never a fabricated one.

`templates/admin/moderation.html`:

* **Queue row** — `ScanStory deleted` (muted text, no anchor) instead of a link.
* **Review modal, project** — `Deleted - no longer available`, and the
  "Open ScanStory" evidence link is hidden (`d-none`) rather than pointed at a
  dead id.
* **Review modal, project state** — `Deleted` (not "Active", which would be a
  lie, and not "Suspended").
* **Review modal, owner** — `Unavailable - ScanStory deleted`, owner link hidden.
* **Reporter / reason / details / status / previous decision** — rendered
  exactly as before. The moderation history is the whole reason the row survives.

No 500, and no `/admin/projects/None`, `/admin/projects/null` or
`/admin/projects/undefined` anywhere in the rendered page — asserted directly
against the response body.

## 14. Reporter / anonymous behaviour

Reporter identity is completely independent of the project's existence.

* A report with `reporter_user_id` set still reports that user id after the
  project is deleted, and `has_reporter_contact` stays `true`. A deleted project
  must never anonymise a reporter, and does not.
* A report that was anonymous stays anonymous — `reporter_user_id: null`,
  `has_reporter_contact: false`. It is not "promoted" to identified, and the
  distinction between "anonymous" and "project deleted" is never conflated.
* `reporter_email`, `reporter_session_hash` and `reporter_ip_hash` are all
  preserved at the row level (verified in the migration lane).

The payload still exposes only `has_reporter_contact` rather than the email
itself — that existing privacy decision was not changed.

## 15. Moderation action behaviour

For a detached report an admin can still: view it in the queue, open it, see
reporter/anonymous state, reason, details, status, and the full
reviewer/resolution history — and can still set `DISMISSED`, `UNDER_REVIEW` or
`ACTION_TAKEN` with a non-suspension action, with `resolution_reason` recorded.

What is refused, and only that:

```python
if status == "ACTION_TAKEN" and action == "PROJECT_SUSPENDED" and report.project is None:
    return jsonify({"success": False, "code": "PROJECT_UNAVAILABLE", "error": ...}), 409
```

`409` with `code: "PROJECT_UNAVAILABLE"` — a business error, not a silent
success and not a 500. Placed with the other validations, **before** any
mutation, so a refused action leaves the report's status and prior resolution
exactly as they were. Without this guard the pre-existing `if project:` made the
suspension a no-op that still returned `success: true` and still wrote
`PROJECT_SUSPENDED` into the audit trail for a project that no longer exists.

The UI also disables the `PROJECT_SUSPENDED` option for a detached report
(re-labelled `Suspend ScanStory (unavailable - deleted)`), so the action is not
offered rather than merely rejected. The backend guard is the authority; the UI
change only avoids a dead-end click.

Suspending a **live** reported project still works unchanged — verified.

## 16. Report history preservation

The surviving `ContentReport` row **is** the moderation history. After a hard
delete it remains queryable by id, listed in `/admin/reports`, readable at
`/admin/reports/<id>`, and still carries who reported, why, what was decided, by
which admin, and when.

No report-deletion path exists anywhere in the application, and a static guard
test asserts none is introduced (`delete(report)`, `ContentReport.query.delete`,
`ContentReport).delete`, `delete(ContentReport` all absent from `app.py`).

## 17. AdminActivity impact

The existing audit mechanism is preserved unchanged — no second audit mechanism
was invented.

* `project_delete` still writes exactly one `AdminActivity` row naming the
  project name, id and owner email. Verified after the fix.
* `content_report_review` still writes one row per review. The only change is
  cosmetic: the detail string now reads `(project deleted)` instead of
  `(project None)` for a detached report.

## 18. Migration upgrade result

Revision `e9b4d7a2c815`, `down_revision = "c1a7f3d95e24"` — a single linear
child of the **actual** current head, verified against the synced tree rather
than assumed (`ScriptDirectory.get_revisions("heads")` returned
`['c1a7f3d95e24']` before, `['e9b4d7a2c815']` after). No historical migration
was edited.

`upgrade()` calls `_reshape(nullable=True, ondelete="SET NULL")`, which is
dialect-aware:

* **PostgreSQL (and any non-SQLite backend)** — plain
  `op.drop_constraint(<reflected name>)` + `op.alter_column(... nullable=True)` +
  `op.create_foreign_key(..., ondelete="SET NULL")`. **No `batch_alter_table`,
  no table recreation** — PostgreSQL supports all three natively, so a table copy
  would be gratuitous. The existing constraint is located by reflection, not by a
  guessed name, mirroring `d4e8b2c6a0f3`'s pattern for the same problem.
* **SQLite only** — `batch_alter_table` with an explicit `copy_from`, because
  SQLite cannot alter a column's nullability at all. `copy_from` is a reflected
  `Table` with the `project_id` FK stripped out; without that, batch mode would
  carry the old unnamed `NO ACTION` rule into the recreated table *alongside* the
  new `SET NULL` one, leaving two conflicting delete rules on the same column
  (which fails outright once `PRAGMA foreign_keys=ON`). Everything else — every
  column, type, default, index and other FK — is reflected as-is.

Measured result: `nullable False → True`, exactly one `project_id` FK named
`fk_content_reports_project_id_projects` with `ondelete: SET NULL`, all nine
indexes intact, all seeded rows present with their `project_id` unchanged.

## 19. Migration downgrade policy

**Policy (A): refuse.** Chosen deliberately over deleting detached rows to make
the rollback succeed — destroying moderation history is the exact data loss this
migration exists to prevent.

```python
detached = SELECT COUNT(*) FROM content_reports WHERE project_id IS NULL
if detached:
    raise RuntimeError("Refusing to downgrade: ... Resolve those rows deliberately first.")
```

This mirrors `c1a7f3d95e24`'s established downgrade-safety convention verbatim
(ownership history refuses rather than guesses), so the repo has one pattern for
this rather than two. Documented in the migration's module docstring and in the
`downgrade()` body.

* **Zero detached rows** → downgrade succeeds cleanly: `project_id` back to
  `NOT NULL`, FK back to no `ON DELETE` clause, all rows and all nine indexes
  retained.
* **Any detached row** → aborts **before** touching anything. Verified: both the
  detached and the live report remain, the column is still nullable, and
  `alembic_version` still reads `e9b4d7a2c815` (a refused downgrade must not
  advance the version).
* **Round trip** upgrade → downgrade → upgrade destroys no report and restores
  the `SET NULL` rule rather than silently losing it.

Operationally this means a rollback is only available until the first project
hard-delete after deploy. That is the correct trade: the alternative is a
rollback that quietly deletes audit evidence.

## 20. PostgreSQL verification

**The migration is written for PostgreSQL and is PostgreSQL-shaped**: on any
non-SQLite backend it takes the native `drop_constraint` / `alter_column` /
`create_foreign_key` path with no table recreation, and it locates the existing
constraint by reflection so the server-assigned
`content_reports_project_id_fkey` is found rather than guessed.

**No live PostgreSQL instance was reachable in this sandbox**, so the
PostgreSQL branch was not executed. The test lane is wired to run there the
moment one is: `tests/migrations/test_content_report_delete_migration.py` reuses
the repo's existing QA-Postgres fixture contract from
`test_migrated_schema_lane.py` (`SCANSTORY_QA_DATABASE_URL` → drop/recreate
`public` schema, run the whole lane against real PostgreSQL). No new
DB-connection mechanism was invented. Every assertion in that file is
backend-agnostic and will execute unchanged against PostgreSQL.

**What was actually proven, and on what engine** — see section 21.

## 21. SQLite / test-environment caveat

Two layers, deliberately proven separately, because they cover different paths.

**Database constraint (`ON DELETE SET NULL`) — proven on SQLite with
`PRAGMA foreign_keys=ON`.** `tests/migrations/test_content_report_delete_migration.py`
enables FK enforcement in its fixture, so the `DELETE FROM projects` assertions
exercise a genuinely enforced foreign key rather than an ignored one. This
matters: SQLite's FK enforcement is opt-in, and that is precisely how the sibling
P0-5 defect stayed invisible through a green suite. Deletes are issued as raw
SQL that never touches the ORM, so what is being measured really is the
constraint.

*Honest limitation:* SQLite's constraint engine is not PostgreSQL's. The
reflected `ondelete: SET NULL`, the nullability change, index retention, row
preservation and the actual detach-on-delete behaviour are all verified — but
verified on SQLite. **PostgreSQL-unverified** specifically: that the native
`ALTER TABLE ... DROP CONSTRAINT` / `ALTER COLUMN ... DROP NOT NULL` /
`ADD CONSTRAINT ... ON DELETE SET NULL` sequence executes without error on a
real PostgreSQL server, and that the reflected constraint name
(`content_reports_project_id_fkey`) is found there. Both are standard
PostgreSQL DDL and mirror an already-deployed migration (`d4e8b2c6a0f3`), but
they were not executed here. **Run this lane against a disposable PostgreSQL
database before production deploy** — see section 27.

**ORM behaviour — proven on the integration suite's SQLite (FKs not enforced).**
`tests/integration/test_content_report_survives_project_delete.py` runs against
the shared `db.create_all()` fixture, which does *not* set
`PRAGMA foreign_keys=ON`. What detaches the report there is SQLAlchemy's default
relationship behaviour (de-associate on parent delete) now that the
`delete-orphan` cascade is gone. That is not a weakness of the test — it is the
second half of the fix, and it is worth pinning independently: had only the
database rule been fixed, the ORM cascade would still have deleted the rows on
PostgreSQL too. Both layers now agree, and both are needed — the DB rule for
coverage of raw/non-ORM paths, the ORM default so no code path has to remember.

This caveat is documented in the docstring of each test file, not only here.

## 22. Focused tests — exact count

**Total: 327 passed, 3 skipped, 0 failed.** The 3 skips are the pre-existing
PostgreSQL-only tests in `test_migrated_schema_lane.py`, which skip loudly rather
than pass vacuously when `SCANSTORY_QA_DATABASE_URL` is unset.

New tests written this lane — **38** (16 migration + 22 application):

| File | Tests | Result |
|---|---|---|
| `tests/migrations/test_content_report_delete_migration.py` *(new)* | 16 | 16 passed |
| `tests/integration/test_content_report_survives_project_delete.py` *(new)* | 22 | 22 passed |

Migration lane — 42 tests, **39 passed, 3 skipped**:

| File | Tests | Result |
|---|---|---|
| `tests/migrations/test_content_report_delete_migration.py` | 16 | 16 passed |
| `tests/migrations/test_alembic_foundation.py` | 6 | 6 passed |
| `tests/migrations/test_migrated_schema_lane.py` | 13 | 10 passed, 3 skipped (Postgres-only) |
| `tests/migrations/test_project_targeted_entitlements_migration.py` | 3 | 3 passed |
| `tests/migrations/test_ownership_history_delete_migration.py` | 4 | 4 passed |

Application lane — **288 passed**:

| File | Tests | Result |
|---|---|---|
| `tests/integration/test_content_report_survives_project_delete.py` | 22 | 22 passed |
| `tests/integration/test_v11_production_ops_admin_investigation.py` (prior production-ops/admin-investigation lane) | 62 | 62 passed |
| `tests/integration/test_domain_commercial_capacity_and_reporting.py` (domain commercial/reporting) | 39 | 39 passed |
| `tests/integration/test_admin_panel_repair.py` (admin-panel-repair) | 8 | 8 passed |
| `tests/integration/test_v11_p0_project_delete_history.py` (admin project-delete) | 17 | 17 passed |
| `tests/integration/test_admin_projects_module.py` | 10 | 10 passed |
| `tests/integration/test_wave1_p0_blockers.py` | 72 | 72 passed |
| `tests/gate_jr/test_v11_commercial_ownership_ux.py` | 58 | 58 passed |

**Coverage against the 15 required migration tests** — all present:
rows kept · `project_id` values kept · nullable · `ON DELETE SET NULL` · indexes
kept · delete sets NULL · delete does not remove the report · unrelated reports
unaffected · moderation metadata preserved · reporter metadata preserved · empty
table · multiple projects/reports · clean downgrade · downgrade refuses on
detached rows · round-trip destroys nothing. Plus one extra: revision graph
linearity.

**Coverage against the 20 required application tests** — all present: detail with
live project · detail after hard delete · reporter still named · anonymous stays
anonymous · reason · details · status · review/resolution metadata · no project
link when deleted · deleted state rendered · no `/admin/projects/None` · queue
lists detached report · detached report still reviewable · `PROJECT_SUSPENDED`
does not falsely succeed · hard delete keeps reports · hard delete still removes
project + media · live report flow green · live suspension flow green ·
permission gates unchanged · report deletion not introduced. Plus two extra:
other projects' reports untouched, and `AdminActivity` on delete unchanged.

One pre-existing assertion was adjusted, not weakened:
`test_ownership_history_delete_migration.py::test_revision_is_the_single_linear_head_on_top_of_wave4`
asserted `c1a7f3d95e24` **was** the Alembic head, which any new migration
necessarily breaks. It now pins what it meant — the history stays linear (exactly
one head, no branch) and `c1a7f3d95e24` remains an ancestor of it — and was
renamed `test_revision_sits_linearly_on_top_of_wave4`.

The full 1900+ suite was **not** run, per instruction.

## 23. Scanner hashes before / after

LF-normalized SHA256, recorded before any edit and re-measured after all edits:

| File | Before | After |
|---|---|---|
| `scanner_runtime.py` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` | `eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0` |
| `static/js/scanner-runtime.js` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` | `05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2` |

**Identical.** No scanner file was opened for editing. No ORB/RANSAC/homography/
optical-flow/tracking/threshold/calibration/overlay code touched, and no scanner
template touched.

## 24. `git diff --check`

Clean — no output, no whitespace errors, no conflict markers.

## 25. `git status --short`

Clean after the final commit — working tree empty, nothing untracked, nothing
stashed.

Immediately before the documentation commit the only entry was:

```
?? V1_1_CONTENT_REPORT_DELETE_INTEGRITY_MIGRATION_REPORT.md
```

Pre-commit state across the whole lane was 4 modified files plus 3 new files, all
listed in section 5.

## 26. Remaining limitations

1. **PostgreSQL DDL not executed** (section 20/21). The strongest remaining gap;
   closed by running one command against a disposable Postgres (section 27).
2. **Downgrade is one-way after the first hard delete.** By design (section 19).
   Detached rows must be resolved deliberately before any rollback.
3. **A detached report shows no historical project name.** `c1a7f3d95e24` added
   `historical_project_id` / `historical_project_name` to the ownership tables so
   a detached row stays human-readable. That was deliberately **not** replicated
   here: it is a schema addition plus an application write at delete time, and
   the approved direction for this lane was explicitly "do not duplicate Project
   data into reports, do not build a moderation-snapshot architecture". A
   moderator opening a detached report sees the reason, reporter, decision and
   timestamps, but not which project name it concerned. If that turns out to
   matter operationally it is a separate, additive change.
4. **Reports on a project deleted *before* this migration are already gone.**
   The fix is forward-looking; it cannot recover history the old cascade
   destroyed.
5. **No report-retention/expiry policy.** Reports now accumulate indefinitely,
   including detached ones. Correct for audit history, but out of scope to bound.

## 27. Production migration / rollback instructions

**Before deploy — close the PostgreSQL gap (recommended, ~2 minutes):**

```bash
createdb scanstory_migration_qa
SCANSTORY_QA_DATABASE_URL=postgresql://<user>@localhost/scanstory_migration_qa \
  venv/Scripts/python.exe -m pytest tests/migrations/test_content_report_delete_migration.py -q
dropdb scanstory_migration_qa
```

Never point `SCANSTORY_QA_DATABASE_URL` at production — the lane drops and
recreates the `public` schema.

**Deploy:**

```bash
# 1. Back up first. The downgrade is conditional (section 19).
pg_dump "$DATABASE_URL" > pre_e9b4d7a2c815.sql

# 2. Confirm the current head is the expected parent.
flask db current          # expect c1a7f3d95e24

# 3. Apply.
flask db upgrade          # -> e9b4d7a2c815
```

Locking: one `ALTER TABLE` taking a brief `ACCESS EXCLUSIVE` lock on
`content_reports`. `DROP NOT NULL` and the FK swap are catalog-only — no table
rewrite, no full scan of `content_reports`, and `ADD CONSTRAINT ... FOREIGN KEY`
validates existing rows against `projects` (they already satisfy it). This is a
small table; expect sub-second. No application downtime required.

**Post-deploy verification:**

```sql
-- expect: is_nullable = YES
SELECT is_nullable FROM information_schema.columns
 WHERE table_name = 'content_reports' AND column_name = 'project_id';

-- expect: fk_content_reports_project_id_projects ... ON DELETE SET NULL
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
 WHERE conrelid = 'content_reports'::regclass AND contype = 'f';

-- expect: unchanged from the pre-deploy count
SELECT COUNT(*) FROM content_reports;
```

**Rollback:**

```bash
# Only succeeds while no report has been detached.
SELECT COUNT(*) FROM content_reports WHERE project_id IS NULL;   -- must be 0
flask db downgrade c1a7f3d95e24
```

If that count is non-zero the downgrade **will refuse** with an explicit
`RuntimeError` and change nothing — this is intended. Options, in order of
preference: (a) stay on `e9b4d7a2c815` and roll back application code only (the
schema is backward-compatible — older code never writes `NULL` and reads the
column fine); (b) export the detached rows for retention, then delete them
deliberately and downgrade; (c) restore `pre_e9b4d7a2c815.sql`. Do **not** work
around the guard by deleting detached rows casually — they are the audit
evidence this change exists to protect.

## 28. Recommendation

**Merge**, with one pre-deploy action.

The defect the prior lane found and deferred is closed at the layer that
actually caused it — and the audit found that layer was *not* where the prior
report assumed. The prior report described "a cascade from `Project`"; reading
the real constraint showed the database FK was plain `NO ACTION` and the
destruction came from the ORM's `cascade="all, delete-orphan"`. Fixing only the
database rule would have left the bug intact. Both layers are now fixed and
agree.

The change is minimal and matches established house patterns throughout:
retention decision copied from `UploadSession` (P0-5), FK-replacement pattern
from `d4e8b2c6a0f3`, downgrade-refusal pattern from `c1a7f3d95e24`, Postgres
fixture contract from `test_migrated_schema_lane.py`. Deletion semantics, media
cleanup, storage accounting, permission gates, `AdminActivity` and the live-
project moderation flow are all unchanged and verified green.

**Pre-deploy action:** run the migration lane once against a disposable
PostgreSQL database (section 27). It is the one claim in this report backed by
SQLite rather than the production engine, and it costs two minutes to close.

Not recommended for this lane, and deliberately left undone: historical project
name on detached reports, report retention policy, and the `Query.get`
deprecations visible throughout `app.py`.
