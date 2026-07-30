# Database Audit

## Detected Database

- SQLAlchemy ORM with Flask-SQLAlchemy.
- MySQL-compatible URI via `DATABASE_URL` in `app.py:63`.
- Pool options configured at `app.py:64` to `app.py:75`.

## Confirmed Query Risks

1. Many relationship defaults use lazy loading.
   - Evidence: `models.py:61`, `models.py:176` to `models.py:180`, `models.py:485` to `models.py:486`.
   - Risk: N+1 behavior in dashboards and admin views.

2. Multiple admin routes load unbounded result sets.
   - Evidence: users `app.py:4271`, subscriptions `app.py:4768`, payments `app.py:4927`, activities `app.py:5313`.

3. Per-project counts are computed in loops.
   - Evidence: dashboard/user projects `app.py:1903` and `app.py:1912`; admin dashboard `app.py:4160` and `app.py:4161`; admin my projects `app.py:4231` and `app.py:4232`.

4. Schema management is in app startup.
   - Evidence: `app.py:239` and `app.py:5762`.
   - Risk: not migration-safe.

## Indexes Seen

- Users/Admin email indexed/unique: `models.py:127`, `models.py:412`.
- Project owner indexes: `models.py:471`, `models.py:472`.
- ProjectPair project/status indexes: `models.py:497`, `models.py:532` to `models.py:534`.
- ScanLog project/user/session indexes: `models.py:658` to `models.py:662`.

## Needed Before Exact Index Recommendations

- Production row counts.
- Slow query log.
- MySQL `EXPLAIN` for admin search, scan history, payments, and dashboard routes.

