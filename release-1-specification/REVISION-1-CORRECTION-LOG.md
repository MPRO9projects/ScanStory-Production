---
title: Revision 1 Correction Log
tags:
  - scan-story/release-1
  - revision-log
status: draft
---

# Revision 1 Correction Log

| Readiness finding | Affected file | Correction made | Canonical rule | Remaining open decision | Blocker status |
|---|---|---|---|---|---|
| Project/Experience terminology conflict | `07`, `current-to-target-model-map.csv` | Added canonical terminology table | Project is legacy; Experience is target | None | Resolved |
| Pair/Trigger terminology conflict | `08`, `current-to-target-model-map.csv` | Added Trigger publication rules | ProjectPair maps to Trigger | None | Resolved |
| Workspace billing unresolved | `07`, `16`, `19`, `24` | Defined Workspace Billing Account and legacy User wrapper | Workspace owns billing | Commercial values configurable | Resolved |
| Legacy QR at risk | `14`, `10`, `27` | Added compatibility resolver and old route preservation | Existing QR and `/scanner/<project_id>` continue working | Retirement after R1 requires approval | Resolved |
| Scanner API at risk | `25`, `27` | Added legacy plus `/api/v1` contract | New scanner changes are versioned | Exact implementation route names can be refined | Resolved |
| Migration rollback vague | `26`, `24`, matrices | Added additive 10-phase migration | No destructive first migration | Queue/storage choices later | Resolved |
| Lifecycle conflicts | `09`, `10`, state CSVs | Replaced lifecycle/state matrices | Published Version immutable; Trigger has canonical states | None | Resolved |
| Partial publish undefined | `08`, `10`, acceptance CSV | Added exclude/active Ready publish rules | Failed active Trigger blocks unless excluded | None | Resolved |
| Creator scalability weak | `15`, performance CSV | Added 1-30/31-100/101-1000 rules | Large Experience design cannot require sequential matching | Full bulk UI deferred | Resolved |
| Mobile fallback partial | `12`, `13` | Added failure matrix | No endless loader | Exact timeout values configurable | Resolved |
| Recognition gates vague | `11` | Added quality robustness runtime gates | Thresholds require calibration | Final numeric values later | Resolved |
| Performance vague | `21`, performance CSV | Added environments and target/baseline/threshold distinction | Unmeasured targets are not current performance | Final thresholds later | Resolved |
| Security high gaps | `20` | Added gate classification | Security controls are pre/staging/prod gates | Implementation remains | Resolved for planning |
| Managed ownership ambiguous | `18`, `07` | Added managed-service ownership rules | Staff work belongs to explicit Workspace | Contract details later | Resolved |
| Gate A not complete | `FINAL` | Kept max classification at planning | Not Ready for implementation before Gate A | Gate A implementation | Remaining blocker |
