# Current Model Dependency Map

Legacy runtime remains centered on `User`, `Admin`, `SubscriptionPlan`, `TrialDetails`, `PaymentOrder`, `OTPCode`, `Project`, `ProjectPair`, `ScanLog`, `UserLoginActivity`, `AdminActivity`, and `SystemConfig`.

`Project.id` is a public scanner identifier for `/scanner/<project_id>`, `/success/<project_id>`, `/project/<project_id>/preview`, QR filenames, QR paths, media routes, and scanner API payloads.

`ProjectPair.project_id` plus `ProjectPair.pair_index` is the legacy marker identity used by image/video routes and `.npz` feature filenames such as `<project_id>_<pair_index>.npz`.

`ProjectPair.id` is the database foreign key used by `ScanLog.pair_id`.

Deletion behavior is cascade-based from `User.projects`, `Admin.projects`, `Project.pairs`, and `Project.scan_logs`.

Billing remains user/subscription-plan based through `User.subscription_id`, `PaymentOrder.user_id`, and `PaymentOrder.plan_id`.

Startup uses `db.create_all()` after importing `models.py`; this makes additive model classes visible to SQLAlchemy metadata without changing existing tables.
