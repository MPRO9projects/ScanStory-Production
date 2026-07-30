# Final Gate C Report

## 1. Executive conclusion

Gate C passed with documented gaps. The repository now has additive compatibility models, public keys, migration services, a local migration CLI, resolvers, tests, and documentation without changing the active legacy scanner/payment/auth behavior.

## 2. Git state

Branch: `gate-c-compatibility-model`. Gate B commit exists: `adea812`. Remote: none.

## 3. Existing model findings

`Project.id` is the active public scanner identity. `ProjectPair.project_id` plus `pair_index` drives media routes and `.npz` filenames. `ProjectPair.id` is the scan-log FK. Billing remains user-plan based.

## 4. New additive models

Organization, Workspace, WorkspaceMember, Experience, ExperienceVersion, Trigger, Asset, TriggerAsset, RecognitionArtifact, ProcessingJob, and MigrationCheckpoint.

## 5. Schema changes

Only new additive tables and nullable mapping fields were introduced. No legacy schema was removed or renamed.

## 6. Default Workspace backfill

Implemented with dry-run, rerun, owner membership creation, checkpoints, invalid-user reporting, and no startup execution.

## 7. Project-to-Experience mapping

Implemented for user-owned projects with owner workspace lookup and one-to-one `legacy_project_id` mapping. Admin-owned projects are reported for explicit resolution.

## 8. Pair-to-Trigger mapping

Implemented for mapped project pairs with one-to-one `legacy_project_pair_id` mapping plus optional asset/artifact representation.

## 9. Asset/artifact representation

Legacy image/video filenames can be represented as local `Asset` records. Legacy `.npz` files can be represented as `RecognitionArtifact` rows. Files are not moved or regenerated.

## 10. Public-key design

Opaque URL-safe public keys are generated centrally, checked for uniqueness, and made immutable.

## 11. Migration checkpoint design

`MigrationCheckpoint` records entity-level completion/failure state by migration name, entity type, and legacy ID.

## 12. Dry-run behavior

Dry-run reports intended creates/skips/errors and does not write target records.

## 13. Idempotency

Reruns reuse existing workspaces, memberships, experiences, triggers, assets, and checkpoints.

## 14. Verification

`verify_gate_c_migration()` reports legacy counts, mapped counts, duplicates, checkpoint failures, orphans, and public-key uniqueness.

## 15. Rollback

Only controlled test rollback is implemented. Production rollback is documented as stop-reading-new-tables plus backup restore if needed.

## 16. Compatibility resolver

Resolver utilities map legacy project/pair IDs to optional target records and target records back to legacy entities. Scanner routes are not wired through the resolver.

## 17. Security and tenant safeguards

Membership uniqueness, legacy mapping uniqueness, public-key uniqueness, status validation, owner workspace mapping, and no silent admin reassignment are covered.

## 18. Production files changed

`models.py`, `public_keys.py`, `compatibility_resolver.py`, `gate_c_migration.py`, and `migration_gate_c.py`.

## 19. Test results

Focused Gate C tests: 20 passed, 0 failed.

Final counted full suite: 80 passed, 4 xfailed, 0 failed, 89 warnings.

## 20. Regression results

Mandatory runner commands passed: fast, contracts, security, full, and full again. Direct `.\run-tests.ps1` is blocked by this workstation's PowerShell policy, so the validated invocation was `powershell -ExecutionPolicy Bypass -File .\run-tests.ps1 <suite>`.

## 21. Real data/media status

Real database not migrated. Real media not moved. `.npz` artifacts not regenerated.

## 22. Known gaps

Admin project ownership resolution, Workspace billing activation, Experience UI, publishing/version switching, queues/workers, and real file migration remain future work.

## 23. Gate C exit-criteria results

Passed with documented gaps. Additive schema, dry-run/apply/verify services, idempotency, public-key behavior, compatibility resolver, legacy QR/scanner/auth/payment regressions, test rollback, documentation, and local-only Git requirements were validated.

## 24. Gate C classification

Gate C passed with documented gaps.

## 25. Exact next gate

Gate D - compatibility migration rehearsal and owner-resolution policy.
