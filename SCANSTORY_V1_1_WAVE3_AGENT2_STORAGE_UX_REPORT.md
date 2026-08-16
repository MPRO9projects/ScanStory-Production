---
title: ScanStory V1.1 Wave 3 Agent 2 Storage UX Report
branch: agent/v1.1-experience-ux
base_backend_commit: bd86ec3
tags:
  - scanstory
  - v1.1
  - wave3
  - storage-ux
---

# ScanStory V1.1 Wave 3 Agent 2 Storage UX Report

## Summary

Aligned creator and admin storage UX with Agent 1's real Wave 3 storage accounting contract.

The UI now renders backend-computed account storage values:

- base storage
- purchased storage
- admin-granted storage
- effective total storage
- used storage
- remaining storage
- over-storage amount

No template or JavaScript calculates entitlement totals locally.

## Backend Contract Consumed

- `user_entitlement_summary(user)` reads `get_effective_entitlements(user)`.
- Storage fields come from:
  - `base_storage_bytes`
  - `purchased_storage_bytes`
  - `admin_granted_storage_bytes`
  - `effective_storage_bytes`
  - `storage_used_bytes`
  - `storage_remaining_bytes`
  - `over_storage`
  - `storage_overage_bytes`
- `ACCOUNT_STORAGE` add-ons are exposed through existing `/api/addons/catalog`.
- Add-on purchase still uses existing `/api/addons/orders` and `/api/addons/purchases/<id>/verify`.
- Admin storage grant uses existing POST `/admin/users/<user_id>/grant-storage`.

## UX Changes

- Profile page shows real storage breakdown and account storage add-on section.
- Dashboard page shows storage used, source split, total and remaining.
- Pricing page no longer claims storage usage is unmeasured.
- Admin user views show storage breakdown, overage copy and governed grant/revoke form.
- Edit-project page explains smaller replacements may be allowed while over storage.
- Resumable upload error copy maps `STORAGE_LIMIT_REACHED` to non-destructive guidance.
- Admin add-on list formats `storage_bytes_delta` as human-readable storage.

## Non-Destructive Copy

Over-storage messaging explicitly says existing projects, media and QR codes remain available. New storage-consuming uploads can be blocked until the user deletes/reduces media, buys storage if configured, or upgrades.

## Tests

Run:

```powershell
& "F:\ScanStory-main\ScanStory-main\venv\Scripts\python.exe" -m pytest tests\integration\test_v1_agent2_admin_parity.py -q
```

Result:

```text
35 passed
```

Run:

```powershell
git -c safe.directory="F:/ScanStory-main/ScanStory-v1.1-agent2" diff --check
```

Result: passed.

## Limitations

- No browser/device QA was run in this checkpoint.
- Storage purchase availability depends on real active `ACCOUNT_STORAGE` catalog rows.
- No payment, entitlement fulfillment, scanner, upload protocol or backend accounting behavior was changed.

## Merge Recommendation

PASS for frontend/admin storage UX integration after normal review.
