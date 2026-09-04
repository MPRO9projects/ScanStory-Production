"""Central effective-entitlement resolver (V1.1 Wave 2).

ONE authoritative answer to "what is this account currently allowed to do".
Every new commercial check routes through get_effective_entitlements(); nothing
re-derives plan math locally.

Two things this module deliberately keeps apart, because conflating them was
called out as a correctness risk:

  * ENTITLEMENT - what the ACCOUNT may currently do (this module).
  * SERVICE COVERAGE - whether a PROJECT's public availability is still paid
    for (ProjectServiceCoverage / apply_standalone_project_renewal in app.py).

Wave 3 update: storage usage is now REAL. `storage_usage_tracked` is True, and
`storage_used_bytes` / `storage_remaining_bytes` / `over_storage` are backed by
the media_objects ledger via storage_accounting.py. Effective storage is the sum
of three separately auditable sources - plan base + purchased ACCOUNT_STORAGE
add-ons + admin grants - and is independent of project capacity, scan allowance,
pair limits, service coverage and experience entitlement.
"""
import os

from sqlalchemy.sql import func

import storage_accounting
from models import (
    PLAN_FAMILY_INDIVIDUAL,
    PLAN_STATUS_ACTIVE,
    EntitlementTransaction,
    db,
)

# ---------------------------------------------------------------------------
# IMMUTABLE SERVER SAFETY CEILINGS.
#
# These are the hard limits the server will enforce no matter what any plan
# says. They are configuration/deployment values, NOT admin-editable plan
# fields - an admin must never be able to raise a plan above them. The
# effective per-file limit is always min(plan policy, ceiling).
#
# These moved here from app.py so the resolver and the upload paths share one
# definition instead of two drifting copies; app.py imports them back.
# ---------------------------------------------------------------------------
MAX_IMAGE_SIZE = int(os.environ.get("MAX_IMAGE_UPLOAD_BYTES", 50 * 1024 * 1024))
MAX_VIDEO_SIZE = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", 1 * 1024 * 1024 * 1024))
MAX_IMAGE_DIMENSION_PX = int(os.environ.get("MAX_IMAGE_DIMENSION_PX", 8000))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", 40_000_000))
# Optional; unset/0 disables the duration check entirely.
MAX_VIDEO_DURATION_SECONDS = int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", "0") or "0") or None
# Hard ceiling on pairs any plan may configure.
MAX_PAIRS_PER_PROJECT_CEILING = int(os.environ.get("MAX_PAIRS_PER_PROJECT_CEILING", "10") or "10")
# Issue 3E-A: hard ceiling on videos-per-target any plan may configure, same
# pattern as the pairs ceiling above. This bounds the plan value only when
# allow_multi_video_per_target is True - see plan_videos_per_target_limit().
MAX_VIDEOS_PER_TARGET_CEILING = int(os.environ.get("MAX_VIDEOS_PER_TARGET_CEILING", "10") or "10")

# Ledger source_type values, split by who paid for the entitlement. Purchased
# and admin-granted entitlement must stay distinguishable and neither may
# silently overwrite the other.
ADMIN_GRANT_SOURCE_TYPE = "admin_grant"
PURCHASE_SOURCE_TYPES = ("addon_purchase", "refund")

# Playback mode -> the SubscriptionPlan column that entitles it.
PLAYBACK_MODE_ENTITLEMENT_FIELDS = {
    "direct": "allow_direct_qr",
    "detect_once": "allow_detect_once",
    "tracked_overlay": "allow_tracked_overlay",
}


def cap(plan_value, ceiling):
    """Effective limit = min(plan policy, hard server ceiling).

    None on either side means "that side imposes no limit". Both None -> None
    (no check at all), which is how the pre-Wave-2 duration check behaved.
    """
    values = [v for v in (plan_value, ceiling) if v not in (None, 0)]
    if not values:
        return None
    return min(int(v) for v in values)


def _ledger_sum(user_id, entitlement_type, source_types=None, exclude_source_types=None):
    q = db.session.query(
        func.coalesce(func.sum(EntitlementTransaction.delta_value), 0)
    ).filter(
        EntitlementTransaction.user_id == user_id,
        EntitlementTransaction.entitlement_type == entitlement_type,
    )
    if source_types is not None:
        q = q.filter(EntitlementTransaction.source_type.in_(tuple(source_types)))
    if exclude_source_types is not None:
        q = q.filter(~EntitlementTransaction.source_type.in_(tuple(exclude_source_types)))
    return int(q.scalar() or 0)


def ledger_breakdown(user, entitlement_type):
    """Purchased vs admin-granted split of one ledger dimension.

    `total` is every row, which is what the materialized User.subscribed_*
    columns are reconciled against - so the split is reporting, never a second
    source of truth that could drift from enforcement.
    """
    if not user:
        return {"purchased": 0, "admin_granted": 0, "total": 0}
    uid = user.id
    admin_granted = _ledger_sum(uid, entitlement_type, source_types=(ADMIN_GRANT_SOURCE_TYPE,))
    total = _ledger_sum(uid, entitlement_type)
    return {
        "purchased": total - admin_granted,
        "admin_granted": admin_granted,
        "total": total,
    }


def allowed_playback_modes(plan):
    """The playback modes this plan may CREATE or CHANGE INTO.

    No plan (trial/lapsed) keeps today's behaviour: everything allowed. This
    never revokes a mode an existing project already has - grandfathering is
    handled by only consulting this on create/change paths.
    """
    if plan is None:
        return set(PLAYBACK_MODE_ENTITLEMENT_FIELDS)
    return {
        mode
        for mode, field in PLAYBACK_MODE_ENTITLEMENT_FIELDS.items()
        if bool(getattr(plan, field, True))
    }


def plan_pairs_limit(plan):
    """Plan pairs-per-project, already capped by the server ceiling.

    None = unlimited (matching get_plan_pairs_limit's pre-existing contract,
    where a NULL/0 plan value meant "not configured").
    """
    raw = getattr(plan, "max_pairs_per_project", None) if plan else None
    try:
        raw = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw = None
    if raw is not None and raw <= 0:
        raw = None
    if raw is None:
        return None
    return cap(raw, MAX_PAIRS_PER_PROJECT_CEILING)


def plan_videos_per_target_limit(plan):
    """Effective videos-per-target ceiling, already capped by the server
    ceiling - None means unbounded-by-plan (still an authoring-side number,
    not "unlimited storage").

    One video per target is the existing base capability regardless of any
    plan, so this is only meaningful when allow_multi_video_per_target is
    True; callers must check that flag first (see get_effective_entitlements),
    not infer it from this return value alone.
    """
    if not plan or not bool(getattr(plan, "allow_multi_video_per_target", False)):
        return None
    raw = getattr(plan, "max_videos_per_target", None)
    try:
        raw = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw = None
    if raw is not None and raw <= 0:
        raw = None
    if raw is None:
        return None
    return cap(raw, MAX_VIDEOS_PER_TARGET_CEILING)


def get_effective_entitlements(user, unlimited_override=False):
    """The one coherent effective view of an account's commercial allowances.

    `unlimited_override` is how callers pass the dev-test entitlement in
    without this module having to import app.py (circular).
    """
    plan = getattr(user, "subscription_plan", None) if user else None

    projects = ledger_breakdown(user, "PROJECT_CAPACITY")
    scans = ledger_breakdown(user, "EXTRA_SCANS")

    # SOURCE OF TRUTH for enforcement stays the materialized User columns -
    # they are what the atomic reservation UPDATEs compare against. The plan +
    # ledger numbers below are the audit view of how they were composed.
    if unlimited_override:
        effective_projects = None
        effective_scans = None
    else:
        raw_projects = getattr(user, "subscribed_project_limit", None) if user else None
        raw_scans = getattr(user, "subscribed_scan_limit", None) if user else None
        effective_projects = None if raw_projects in (None, 0) else int(raw_projects)
        effective_scans = None if raw_scans in (None, 0) else int(raw_scans)

    projects_used = int(getattr(user, "projects_used", 0) or 0) if user else 0
    scans_used = int(getattr(user, "scans_used", 0) or 0) if user else 0

    if unlimited_override:
        modes = set(PLAYBACK_MODE_ENTITLEMENT_FIELDS)
        pairs = None
        allow_multi_video = True
        videos_per_target = None
    else:
        modes = allowed_playback_modes(plan)
        pairs = plan_pairs_limit(plan)
        allow_multi_video = bool(getattr(plan, "allow_multi_video_per_target", False)) if plan else False
        videos_per_target = plan_videos_per_target_limit(plan) if allow_multi_video else None

    # One video per target is the existing base capability regardless of
    # plan - a disabled/unset feature must resolve to exactly 1, never to
    # "no limit", or a caller that reads this number without also checking
    # the flag would silently allow unlimited videos on a plan that never
    # entitled the feature at all.
    effective_videos_per_target = 1 if not allow_multi_video else (videos_per_target if videos_per_target is not None else None)

    # --- storage: three separate, auditable sources -----------------------
    # base (plan) + purchased (ACCOUNT_STORAGE add-ons) + admin grants. None
    # base means "this plan states no storage allowance", which is NOT a claim
    # of unlimited storage but is treated as unenforced - the same reading
    # is_downgrade() already applies. Purchased/granted bytes on top of an
    # unstated base therefore also leave the account unenforced; a plan must
    # state a base before storage can be metered.
    base_storage = getattr(plan, "base_storage_bytes", None) if plan else None
    storage = ledger_breakdown(user, "ACCOUNT_STORAGE")
    purchased_storage = storage["purchased"]
    granted_storage = storage["admin_granted"]
    if unlimited_override or base_storage is None:
        effective_storage = None
    else:
        effective_storage = max(0, int(base_storage) + purchased_storage + granted_storage)
    storage_used = storage_accounting.stored_storage_used_bytes(user)
    over_storage = effective_storage is not None and storage_used > effective_storage

    return {
        # --- plan identity / lifecycle ---
        "plan_id": getattr(plan, "id", None),
        "plan_name": getattr(plan, "plan_name", None),
        "plan_family": getattr(plan, "plan_family", None) or PLAN_FAMILY_INDIVIDUAL,
        "plan_revision": int(getattr(plan, "plan_revision", 1) or 1),
        "plan_lifecycle_status": getattr(plan, "lifecycle_status", None) or PLAN_STATUS_ACTIVE,
        "plan_is_purchasable": bool(getattr(plan, "is_purchasable", False)) if plan else False,

        # --- project capacity ---
        "base_project_limit": getattr(plan, "total_project_limit", None) if plan else None,
        "purchased_project_capacity": projects["purchased"],
        "admin_granted_project_capacity": projects["admin_granted"],
        "effective_project_limit": effective_projects,
        "projects_used": projects_used,
        "projects_remaining": None if effective_projects is None else max(0, effective_projects - projects_used),
        "over_project_capacity": False if effective_projects is None else projects_used > effective_projects,

        # --- scans ---
        "base_scan_limit": getattr(plan, "total_scan_limit", None) if plan else None,
        "purchased_scan_capacity": scans["purchased"],
        "admin_granted_scan_capacity": scans["admin_granted"],
        "effective_scan_limit": effective_scans,
        "scans_used": scans_used,
        "scans_remaining": None if effective_scans is None else max(0, effective_scans - scans_used),

        # --- pairs (server ceiling already applied) ---
        "max_pairs_per_project": pairs,

        # --- multi-video-per-target (Issue 3E-A). Foundation only - nothing
        # yet consumes this; PairMedia/add-video enforcement is a later phase.
        # effective_max_videos_per_target is always 1 when the flag is False,
        # never "unlimited" - see the note above where these are computed.
        "allow_multi_video_per_target": allow_multi_video,
        "max_videos_per_target": getattr(plan, "max_videos_per_target", None) if plan else None,
        "effective_max_videos_per_target": effective_videos_per_target,

        # --- storage entitlement AND usage (Wave 3) ---
        "base_storage_bytes": base_storage,
        "purchased_storage_bytes": purchased_storage,
        "admin_granted_storage_bytes": granted_storage,
        "effective_storage_bytes": effective_storage,
        "storage_used_bytes": storage_used,
        "storage_remaining_bytes": None if effective_storage is None else max(0, effective_storage - storage_used),
        # Flipped True in Wave 3: the UI's "entitlement only, not tracked yet"
        # disclaimer is now obsolete and these numbers are real.
        "storage_usage_tracked": True,
        "over_storage": over_storage,
        "storage_overage_bytes": max(0, storage_used - effective_storage) if effective_storage is not None else 0,

        # --- experience entitlements ---
        "allow_direct_qr": "direct" in modes,
        "allow_detect_once": "detect_once" in modes,
        "allow_tracked_overlay": "tracked_overlay" in modes,
        "allowed_playback_modes": modes,

        # --- per-file media policy, hard ceiling already applied ---
        "image_policy": {
            "max_bytes": cap(getattr(plan, "max_image_bytes", None) if plan else None, MAX_IMAGE_SIZE),
            "max_dimension_px": cap(getattr(plan, "max_image_dimension_px", None) if plan else None, MAX_IMAGE_DIMENSION_PX),
            "max_pixels": cap(getattr(plan, "max_image_pixels", None) if plan else None, MAX_IMAGE_PIXELS),
        },
        "video_policy": {
            "max_bytes": cap(getattr(plan, "max_video_bytes", None) if plan else None, MAX_VIDEO_SIZE),
            "max_duration_seconds": cap(
                getattr(plan, "max_video_duration_seconds", None) if plan else None,
                MAX_VIDEO_DURATION_SECONDS,
            ),
        },

        # --- account / term state ---
        "subscription_status": getattr(user, "subscription_status", None) if user else None,
        "subscription_expires_at": getattr(user, "subscription_expires_at", None) if user else None,
        "has_active_subscription": bool(user.has_active_subscription()) if user else False,
        "pending_plan_id": getattr(user, "pending_plan_id", None) if user else None,
        "pending_plan_effective_at": getattr(user, "pending_plan_effective_at", None) if user else None,

        "unlimited": bool(unlimited_override),
    }


def image_limits(entitlements):
    """(max_bytes, max_dimension_px, max_pixels) in validate_image()'s order."""
    p = entitlements["image_policy"]
    return p["max_bytes"], p["max_dimension_px"], p["max_pixels"]


def video_limits(entitlements):
    """(max_bytes, max_duration_seconds) in validate_video()'s order."""
    p = entitlements["video_policy"]
    return p["max_bytes"], p["max_duration_seconds"]


def is_downgrade(current_plan, new_plan):
    """Does new_plan represent a strictly LOWER commercial policy?

    Used to decide whether a confirmed paid plan change applies immediately
    (upgrade / like-for-like) or is deferred to the next term boundary
    (downgrade). None/0 on a numeric limit means unlimited, i.e. the highest
    possible value - so moving from unlimited to a finite number is a
    downgrade, and the reverse is not.
    """
    if current_plan is None or new_plan is None or current_plan.id == new_plan.id:
        return False

    def _rank(value):
        # None/0 == unlimited == infinitely high.
        return float("inf") if value in (None, 0) else int(value)

    for field in ("total_project_limit", "total_scan_limit", "max_pairs_per_project"):
        if _rank(getattr(new_plan, field, None)) < _rank(getattr(current_plan, field, None)):
            return True

    # Losing ANY experience entitlement is a downgrade (set difference, not a
    # strict-subset test - swapping one premium mode for another still removes
    # something the account currently has).
    if allowed_playback_modes(current_plan) - allowed_playback_modes(new_plan):
        return True

    # Multi-video-per-target (Issue 3E-A): losing the feature entirely is a
    # downgrade; keeping it but lowering the plan's max is a downgrade under
    # the same None/0-is-unlimited convention as the numeric fields above.
    old_multi_video = bool(getattr(current_plan, "allow_multi_video_per_target", False))
    new_multi_video = bool(getattr(new_plan, "allow_multi_video_per_target", False))
    if old_multi_video and not new_multi_video:
        return True
    if old_multi_video and new_multi_video:
        if _rank(getattr(new_plan, "max_videos_per_target", None)) < _rank(getattr(current_plan, "max_videos_per_target", None)):
            return True

    # Storage: None means "unspecified", which is not a claim of unlimited
    # storage - only compare when BOTH sides state a number.
    old_storage = getattr(current_plan, "base_storage_bytes", None)
    new_storage = getattr(new_plan, "base_storage_bytes", None)
    if old_storage is not None and new_storage is not None and int(new_storage) < int(old_storage):
        return True

    return False
