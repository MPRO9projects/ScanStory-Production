# Rehearsal Database Strategy

Gate D used disposable SQLite rehearsal databases created by `gate_d_build_rehearsal_db.py`.

The data is masked and synthetic. It includes normal/trial/paid/expired/inactive users, admins, user-owned projects, admin-owned projects, unknown-owner projects, projects with multiple pairs, missing media-path examples, scan logs, payments, plans, trials, and activity records.

No production credentials or real customer data were used.

Tested sizes:

- small: 10 users, 20 projects, 50 pairs
- medium: 30 users, 90 projects, 250 pairs
- large: 60 users, 180 projects, 500 pairs
