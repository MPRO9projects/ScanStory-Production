# Gate H Exit Criteria

Gate H passed with documented gaps.

Satisfied:

- Gate G committed.
- Gate H branch used.
- Publishing flags disabled by default.
- Draft and Published Versions separated.
- Published snapshots immutable.
- First and subsequent publication tested.
- Same QR serves Video B after new publication and Video A after rollback.
- Pause, resume, archive, fallback states tested.
- Authorization, idempotency, and cross-Workspace denial tested.
- Legacy QR/scanner/auth/payment regressions green.
- No real DB migration, customer publishing, media movement, Workspace billing, remote, AWS, S3, Redis, or Celery.
