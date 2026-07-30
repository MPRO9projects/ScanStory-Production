# Final Gate H Report

## 1. Executive conclusion

Gate H passed with documented gaps. Publishing, immutable snapshots, permanent public identity, rollback, pause/resume/archive, and controlled public resolution are implemented behind disabled-by-default flags.

## 2. Git state

Root `F:/ScanStory-main/ScanStory-main`, branch `gate-h-publishing-versioning`, no remote, Gate G committed.

## 3. Feature flags

Added disabled-by-default flags: `ENABLE_EXPERIENCE_PUBLISHING`, `ENABLE_PUBLIC_EXPERIENCE_ROUTE`, `ENABLE_VERSION_ROLLBACK`, `ENABLE_EXPERIENCE_PAUSE`.

## 4. Version model

`ExperienceVersion` was extended additively for publication metadata and immutable publication state.

## 5. Draft creation

`ensure_draft_version()` creates an editable Draft Version or returns the existing one.

## 6. Published immutability

Published snapshots are marked immutable and guarded against update/delete.

## 7. Trigger snapshot

`ExperienceVersionTrigger` stores exact Trigger and asset references for each Version.

## 8. Publish readiness

Readiness blocks missing media, missing recognition, active failed Triggers, active processing jobs, and no active included Triggers.

## 9. Atomic publication

Publication swaps `current_published_version_id` only after validation and snapshot finalization.

## 10. Permanent public identity

Public route uses `/e/<experience_public_key>`.

## 11. Master QR activation

Master QR destination remains the permanent public route and does not change during publication or rollback.

## 12. Same-QR content update

Automated test verifies Video A -> Draft Video B -> publish -> same QR serves Video B -> rollback -> same QR serves Video A.

## 13. Public scanner resolution

Resolver returns only current Published Version snapshots and renders a controlled compatibility shell.

## 14. Rollback

Rollback restores previous immutable Published Version snapshots through the same public key.

## 15. Pause/resume/archive

Pause returns `503`, resume restores `200`, archive returns `410`.

## 16. Public fallback states

Disabled, unknown, unpublished, paused, archived, and unavailable states are handled safely.

## 17. Authorization

Owner/admin/publisher can publish; creator can draft but cannot publish; cross-Workspace access is denied.

## 18. Concurrency and idempotency

Repeated publication with the same idempotency key returns the same Version. Full row locking remains a documented gap.

## 19. Audit and observability

Publication lifecycle events are recorded in ProcessingEvent.

## 20. Creator UI integration

Gate G detail UI now shows publish readiness, Version history, publish, rollback, pause, resume, archive, and permanent public link controls when flags are enabled.

## 21. Security

No Draft leakage, no internal diagnostics in public output, opaque public keys, and server-side authorization are covered. Existing security xfails remain.

## 22. Performance

1, 30, and 100 Trigger publish/public resolver tests passed.

## 23. Production files changed

`models.py`, `feature_flags.py`, `publishing.py`, `experience_creator.py`, and Experience templates.

## 24. Test results

Final quiet suite: `142 passed, 4 xfailed`.

## 25. Manual validation status

Manual mobile/browser validation not executed.

## 26. Regression results

Fast, contracts, security, full twice, and final quiet suite passed.

## 27. Real data/media status

No real DB migration. No real customer publishing. No real media movement.

## 28. Known gaps

Full DB row locks, physical QR/camera validation, and 10/50 Version perf rehearsals remain documented gaps.

## 29. Gate H exit criteria

Satisfied with documented gaps.

## 30. Gate H classification

Gate H passed with documented gaps.

## 31. Exact next gate

Gate I - production migration planning, controlled rollout, and scanner hardening.
