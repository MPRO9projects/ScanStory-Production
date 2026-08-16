"""Wave 2 tests: commercial entitlement foundation.

Covers the SubscriptionPlan policy fields, the central effective-entitlement
resolver, admin-grant normalisation, per-file media policy under the immutable
server ceilings, pair/experience grandfathering, upgrade validity chaining and
the deferred (next-term) downgrade lifecycle.
"""
from datetime import datetime, timedelta

import pytest

import entitlements as ent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _paid_plan(app_module):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()


def _new_plan(app_module, db_session, name, **kwargs):
    fields = dict(
        plan_name=name,
        plan_amount=100.0,
        duration_type="time",
        duration_value=12,
        total_project_limit=5,
        total_scan_limit=500,
        max_pairs_per_project=5,
        is_trial_plan=False,
        is_active=True,
    )
    fields.update(kwargs)
    plan = app_module.SubscriptionPlan(**fields)
    db_session.add(plan)
    db_session.commit()
    return plan


def _pending_order(app_module, db_session, user, plan, order_id):
    order = app_module.PaymentOrder(
        order_id=order_id,
        razorpay_order_id=f"rzp_{order_id}",
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.effective_price,
        currency=plan.currency,
        status="pending",
        purchased_project_limit=plan.total_project_limit,
        purchased_scan_limit=plan.total_scan_limit,
    )
    db_session.add(order)
    db_session.commit()
    return order


def _ledger(app_module, db_session, user, entitlement_type, delta, source_id, source_type="addon_purchase"):
    row = app_module.EntitlementTransaction(
        user_id=user.id,
        entitlement_type=entitlement_type,
        delta_value=delta,
        source_type=source_type,
        source_id=source_id,
        reason="test fixture",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _ents(app_module, user):
    return app_module.user_entitlements(user)


# ===========================================================================
# Plan family
# ===========================================================================
def test_plan_family_defaults_to_individual(app_module, db_session):
    """Existing plans keep working: the safe inferred default, not an invention."""
    from models import PLAN_FAMILIES, PLAN_FAMILY_INDIVIDUAL

    plan = _new_plan(app_module, db_session, "Family Default")
    assert plan.plan_family == PLAN_FAMILY_INDIVIDUAL
    assert PLAN_FAMILIES == {"INDIVIDUAL", "BUSINESS_VENDOR"}


def test_every_existing_seeded_plan_has_a_family(app_module, db_session):
    from models import PLAN_FAMILIES

    plans = app_module.SubscriptionPlan.query.all()
    assert plans, "expected seeded plans"
    for plan in plans:
        assert plan.plan_family in PLAN_FAMILIES
        assert plan.lifecycle_status == "ACTIVE"


def test_plan_family_business_vendor_is_accepted(app_module, db_session):
    plan = _new_plan(app_module, db_session, "Vendor Plan", plan_family="BUSINESS_VENDOR")
    assert plan.plan_family == "BUSINESS_VENDOR"


def test_plan_family_is_normalised_and_invalid_family_rejected(app_module, db_session):
    plan = _new_plan(app_module, db_session, "Lowercase Family", plan_family="business_vendor")
    assert plan.plan_family == "BUSINESS_VENDOR"
    with pytest.raises(ValueError):
        _new_plan(app_module, db_session, "Bad Family", plan_family="ENTERPRISE_GALAXY")


def test_plan_family_appears_in_effective_resolver(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Vendor Resolver", plan_family="BUSINESS_VENDOR")
    normal_user.subscription_id = plan.id
    db_session.commit()
    assert _ents(app_module, normal_user)["plan_family"] == "BUSINESS_VENDOR"


# ===========================================================================
# Plan lifecycle / versioning foundation
# ===========================================================================
def test_plan_lifecycle_defaults_to_active_and_is_purchasable(app_module, db_session):
    plan = _new_plan(app_module, db_session, "Lifecycle Default")
    assert plan.lifecycle_status == "ACTIVE"
    assert plan.plan_revision == 1
    assert plan.is_purchasable is True


def test_plan_closed_for_new_purchase_is_not_purchasable(app_module, db_session):
    plan = _new_plan(app_module, db_session, "Closed", lifecycle_status="CLOSED_FOR_NEW_PURCHASE")
    assert plan.is_purchasable is False


def test_invalid_lifecycle_status_rejected(app_module, db_session):
    with pytest.raises(ValueError):
        _new_plan(app_module, db_session, "Bad Lifecycle", lifecycle_status="SOMEDAY")


def test_activation_snapshots_the_agreed_commercial_policy(app_module, db_session, normal_user):
    """A later admin edit to a live plan must not rewrite what past customers bought."""
    plan = _new_plan(app_module, db_session, "Snapshot Plan", total_scan_limit=400)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_SNAP")

    app_module.activate_payment(order)

    refreshed = app_module.PaymentOrder.query.get(order.id)
    snapshot = refreshed.plan_policy_snapshot
    assert snapshot["total_scan_limit"] == 400
    assert snapshot["plan_revision"] == 1
    assert snapshot["plan_family"] == "INDIVIDUAL"

    # Mutating the live plan afterwards leaves the historical record intact.
    plan.total_scan_limit = 5
    plan.plan_revision = 2
    db_session.commit()
    assert app_module.PaymentOrder.query.get(order.id).plan_policy_snapshot["total_scan_limit"] == 400


# ===========================================================================
# Experience entitlements
# ===========================================================================
@pytest.mark.parametrize(
    "mode,field",
    [("direct", "allow_direct_qr"), ("detect_once", "allow_detect_once"), ("tracked_overlay", "allow_tracked_overlay")],
)
def test_experience_modes_allowed_by_default(app_module, db_session, mode, field):
    plan = _new_plan(app_module, db_session, f"Allows {mode}")
    assert getattr(plan, field) is True
    assert mode in ent.allowed_playback_modes(plan)


@pytest.mark.parametrize(
    "mode,field",
    [("direct", "allow_direct_qr"), ("detect_once", "allow_detect_once"), ("tracked_overlay", "allow_tracked_overlay")],
)
def test_experience_mode_can_be_blocked_by_plan(app_module, db_session, mode, field):
    plan = _new_plan(app_module, db_session, f"Blocks {mode}", **{field: False})
    assert mode not in ent.allowed_playback_modes(plan)


def test_resolver_reports_experience_entitlements(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "No Direct QR", allow_direct_qr=False)
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["allow_direct_qr"] is False
    assert e["allow_detect_once"] is True
    assert e["allow_tracked_overlay"] is True


def test_entitled_mode_resolves(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "All Modes")
    normal_user.subscription_id = plan.id
    db_session.commit()
    with app_module.app.test_request_context():
        assert app_module._resolve_project_experience_playback(
            "image_video", "detect_once", user=normal_user
        ) == ("image_video", "detect_once")


def test_non_entitled_mode_is_blocked_on_create(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "No Detect Once", allow_detect_once=False)
    normal_user.subscription_id = plan.id
    db_session.commit()
    with app_module.app.test_request_context():
        with pytest.raises(ValueError, match="does not include"):
            app_module._resolve_project_experience_playback("image_video", "detect_once", user=normal_user)


def test_invalid_experience_playback_pairing_still_rejected_regardless_of_plan(app_module, db_session, normal_user):
    """Entitlement must never turn an *invalid combination* into a valid one."""
    plan = _new_plan(app_module, db_session, "Everything Allowed")
    normal_user.subscription_id = plan.id
    db_session.commit()
    with app_module.app.test_request_context():
        with pytest.raises(ValueError, match="not supported"):
            app_module._resolve_project_experience_playback("direct_qr", "tracked_overlay", user=normal_user)
        with pytest.raises(ValueError, match="not supported"):
            app_module._resolve_project_experience_playback("image_video", "direct", user=normal_user)


def test_grandfathered_project_keeps_mode_after_entitlement_removed(app_module, db_session, normal_user):
    """A downgrade never rewrites an existing project's playback mode."""
    plan = _new_plan(app_module, db_session, "Started Premium")
    normal_user.subscription_id = plan.id
    db_session.commit()
    project = app_module.Project(
        name="Grandfathered",
        owner_user_id=normal_user.id,
        experience_type="image_video",
        playback_mode="detect_once",
    )
    db_session.add(project)
    db_session.commit()

    plan.allow_detect_once = False
    db_session.commit()

    # Untouched: no revocation, no rewrite, still serving.
    assert app_module.Project.query.get(project.id).playback_mode == "detect_once"
    # But changing INTO the now-unentitled mode is blocked.
    with app_module.app.test_request_context():
        with pytest.raises(ValueError, match="does not include"):
            app_module._resolve_project_experience_playback("image_video", "detect_once", user=normal_user)


def test_changing_into_another_non_entitled_premium_mode_is_blocked(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Only Tracked", allow_detect_once=False, allow_direct_qr=False)
    normal_user.subscription_id = plan.id
    db_session.commit()
    with app_module.app.test_request_context():
        assert app_module._resolve_project_experience_playback(
            "image_video", "tracked_overlay", user=normal_user
        )[1] == "tracked_overlay"
        with pytest.raises(ValueError):
            app_module._resolve_project_experience_playback("direct_qr", "direct", user=normal_user)


# ===========================================================================
# Per-file media policy and the immutable server safety ceilings
# ===========================================================================
def test_plan_below_ceiling_is_the_effective_limit(app_module, db_session, normal_user):
    plan = _new_plan(
        app_module, db_session, "Small Media",
        max_image_bytes=1024, max_video_bytes=2048,
        max_image_dimension_px=100, max_image_pixels=5000,
        max_video_duration_seconds=30,
    )
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["image_policy"] == {"max_bytes": 1024, "max_dimension_px": 100, "max_pixels": 5000}
    assert e["video_policy"] == {"max_bytes": 2048, "max_duration_seconds": 30}


def test_plan_above_ceiling_is_capped_by_the_server(app_module, db_session, normal_user):
    """An admin must NEVER be able to raise a plan past the hard safety ceiling."""
    plan = _new_plan(
        app_module, db_session, "Greedy Media",
        max_image_bytes=ent.MAX_IMAGE_SIZE * 100,
        max_video_bytes=ent.MAX_VIDEO_SIZE * 100,
        max_image_dimension_px=ent.MAX_IMAGE_DIMENSION_PX * 100,
        max_image_pixels=ent.MAX_IMAGE_PIXELS * 100,
    )
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["image_policy"]["max_bytes"] == ent.MAX_IMAGE_SIZE
    assert e["image_policy"]["max_dimension_px"] == ent.MAX_IMAGE_DIMENSION_PX
    assert e["image_policy"]["max_pixels"] == ent.MAX_IMAGE_PIXELS
    assert e["video_policy"]["max_bytes"] == ent.MAX_VIDEO_SIZE


def test_null_plan_media_policy_preserves_pre_wave2_behaviour(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "No Media Policy")
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["image_policy"]["max_bytes"] == ent.MAX_IMAGE_SIZE
    assert e["video_policy"]["max_bytes"] == ent.MAX_VIDEO_SIZE
    assert e["video_policy"]["max_duration_seconds"] == ent.MAX_VIDEO_DURATION_SECONDS


def test_cap_helper_semantics():
    assert ent.cap(None, 10) == 10        # plan silent -> ceiling rules
    assert ent.cap(3, 10) == 3            # plan stricter -> plan rules
    assert ent.cap(99, 10) == 10          # plan greedier -> ceiling rules
    assert ent.cap(None, None) is None    # neither -> no check at all
    assert ent.cap(5, None) == 5


def test_image_and_video_limit_tuples_match_validator_signatures(app_module, db_session, normal_user):
    e = _ents(app_module, normal_user)
    assert len(ent.image_limits(e)) == 3   # validate_image(max_bytes, max_dim, max_pixels)
    assert len(ent.video_limits(e)) == 2   # validate_video(max_bytes, max_duration)


# ===========================================================================
# Pairs: limit, ceiling, grandfathering
# ===========================================================================
def test_pairs_limit_is_capped_by_server_ceiling(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Greedy Pairs", max_pairs_per_project=9999)
    normal_user.subscription_id = plan.id
    db_session.commit()
    assert app_module.get_plan_pairs_limit(normal_user) == ent.MAX_PAIRS_PER_PROJECT_CEILING
    assert _ents(app_module, normal_user)["max_pairs_per_project"] == ent.MAX_PAIRS_PER_PROJECT_CEILING


def test_pairs_limit_below_ceiling_is_respected(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Three Pairs", max_pairs_per_project=3)
    normal_user.subscription_id = plan.id
    db_session.commit()
    assert app_module.get_plan_pairs_limit(normal_user) == 3


def _project_with_pairs(app_module, db_session, user, count):
    project = app_module.Project(name="Pairs", owner_user_id=user.id)
    db_session.add(project)
    db_session.commit()
    for i in range(count):
        db_session.add(app_module.ProjectPair(project_id=project.id, pair_index=i, video_filename=f"v{i}.mp4"))
    db_session.commit()
    return project


def test_pair_add_under_limit_succeeds(app_module, db_session, normal_user):
    project = _project_with_pairs(app_module, db_session, normal_user, 1)
    ok, err = app_module._reserve_pair_slots_for_project(project.id, 1, 3)
    assert ok is True and err is None


def test_pair_add_at_limit_is_blocked(app_module, db_session, normal_user):
    project = _project_with_pairs(app_module, db_session, normal_user, 3)
    ok, err = app_module._reserve_pair_slots_for_project(project.id, 1, 3)
    assert ok is False and "maximum 3 pairs" in err


def test_over_limit_grandfathered_project_keeps_its_pairs_but_cannot_grow(app_module, db_session, normal_user):
    """Lowering a plan's pair limit must never delete pairs."""
    project = _project_with_pairs(app_module, db_session, normal_user, 6)
    ok, err = app_module._reserve_pair_slots_for_project(project.id, 1, 2)
    assert ok is False
    # Nothing deleted, nothing rewritten.
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 6


def test_replacement_is_pair_count_neutral(app_module, db_session, normal_user):
    """Replacing media on an over-limit project adds no pair, so it is allowed."""
    project = _project_with_pairs(app_module, db_session, normal_user, 6)
    before = app_module.ProjectPair.query.filter_by(project_id=project.id).count()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id).first()
    pair.video_filename = "replaced.mp4"
    db_session.commit()
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == before
    # requesting 0 new pairs is always fine, even while over the limit
    assert app_module._reserve_pair_slots_for_project(project.id, 0, 2)[0] is True


# ===========================================================================
# Entitlement composition: plan vs purchased vs admin grant
# ===========================================================================
def test_resolver_reports_base_purchased_and_effective_project_capacity(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "PROJECT_CAPACITY", 4, source_id=7101)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_PROJ")
    app_module.activate_payment(order)

    e = _ents(app_module, app_module.User.query.get(normal_user.id))
    assert e["base_project_limit"] == plan.total_project_limit
    assert e["purchased_project_capacity"] == 4
    assert e["effective_project_limit"] == plan.total_project_limit + 4


def test_purchased_scans_survive_activation_and_are_reported_separately(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 300, source_id=7102)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_SCANS")
    app_module.activate_payment(order)

    e = _ents(app_module, app_module.User.query.get(normal_user.id))
    assert e["purchased_scan_capacity"] == 300
    assert e["admin_granted_scan_capacity"] == 0
    assert e["effective_scan_limit"] == plan.total_scan_limit + 300


def test_admin_grant_is_distinguishable_from_purchased_entitlement(app_module, db_session, normal_user):
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 100, source_id=7103)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 50, source_id=7104,
            source_type=ent.ADMIN_GRANT_SOURCE_TYPE)

    e = _ents(app_module, normal_user)
    assert e["purchased_scan_capacity"] == 100
    assert e["admin_granted_scan_capacity"] == 50


def test_admin_grant_does_not_erase_purchased_entitlement_on_activation(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 200, source_id=7105)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 75, source_id=7106,
            source_type=ent.ADMIN_GRANT_SOURCE_TYPE)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_MIX")
    app_module.activate_payment(order)

    user = app_module.User.query.get(normal_user.id)
    assert user.subscribed_scan_limit == plan.total_scan_limit + 200 + 75


def test_admin_setting_a_scan_limit_preserves_purchased_entitlement(app_module, db_session, normal_user):
    """Regression: the admin route used to assign the column directly, deleting
    entitlement the user had paid for."""
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 500, source_id=7107)
    app_module.materialize_plan_entitlements(normal_user, plan_scan_limit=1000)
    db_session.commit()
    assert normal_user.subscribed_scan_limit == 1500


def test_base_storage_entitlement_appears_in_resolver_but_is_not_metered(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Storage Plan", base_storage_bytes=10 * 1024 ** 3)
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["base_storage_bytes"] == 10 * 1024 ** 3
    assert e["effective_storage_bytes"] == 10 * 1024 ** 3
    # Wave 3 owns usage accounting; Wave 2 must not pretend to have it.
    assert e["purchased_storage_bytes"] == 0
    assert e["storage_usage_tracked"] is False


def test_storage_entitlement_uses_64_bit_column(app_module, db_session):
    """Integer would silently cap byte counts at ~2.1GB (Wave 1 audit finding)."""
    big = 50 * 1024 ** 3  # 50 GiB, far past a 32-bit int
    plan = _new_plan(app_module, db_session, "Big Storage", base_storage_bytes=big,
                     max_video_bytes=big, max_image_pixels=big)
    db_session.expire_all()
    refreshed = app_module.SubscriptionPlan.query.get(plan.id)
    assert refreshed.base_storage_bytes == big
    assert refreshed.max_video_bytes == big
    assert refreshed.max_image_pixels == big


def test_resolver_shape_is_stable_for_wave3_storage(app_module, db_session, normal_user):
    """Adding purchased storage in Wave 3 must not need a breaking change."""
    e = _ents(app_module, normal_user)
    for key in ("base_storage_bytes", "purchased_storage_bytes", "effective_storage_bytes",
                "plan_family", "max_pairs_per_project", "image_policy", "video_policy",
                "allowed_playback_modes", "effective_project_limit", "effective_scan_limit",
                "admin_granted_scan_capacity", "pending_plan_id", "pending_plan_effective_at"):
        assert key in e, f"resolver contract lost {key}"


# ===========================================================================
# Upgrade: immediate, validity chained, idempotent
# ===========================================================================
def test_upgrade_is_effective_immediately(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_UP")
    app_module.activate_payment(order)
    user = app_module.User.query.get(normal_user.id)
    assert user.subscription_status == "active"
    assert user.subscription_id == plan.id
    assert user.pending_plan_id is None


def test_upgrade_chains_unused_paid_validity(app_module, db_session, normal_user):
    """The deferred Wave 1 decision: remaining paid time is preserved, not binned."""
    low = _new_plan(app_module, db_session, "Low", total_project_limit=2, total_scan_limit=100,
                    max_pairs_per_project=2, duration_value=12)
    high = _new_plan(app_module, db_session, "High", total_project_limit=20, total_scan_limit=1000,
                     max_pairs_per_project=8, duration_value=12)

    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, low, "ORD_W2_CHAIN_A"))
    user = app_module.User.query.get(normal_user.id)
    first_end = user.subscription_expires_at
    remaining = first_end - datetime.utcnow()
    assert remaining.days > 300

    app_module.activate_payment(_pending_order(app_module, db_session, user, high, "ORD_W2_CHAIN_B"))
    user = app_module.User.query.get(normal_user.id)

    # New 12-month term APPENDED to the ~12 months still unused.
    total_days = (user.subscription_expires_at - datetime.utcnow()).days
    assert total_days >= 720, f"unused paid validity was discarded ({total_days} days)"
    assert user.subscription_expires_at > first_end


def test_trial_validity_is_not_chained(app_module, db_session, normal_user):
    """Only PAID validity chains - a trial has none to preserve."""
    assert normal_user.subscription_status == "trial"
    normal_user.subscription_expires_at = datetime.utcnow() + timedelta(days=3650)
    db_session.commit()
    plan = _new_plan(app_module, db_session, "Post Trial", duration_value=12)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, plan, "ORD_W2_TRIAL"))
    user = app_module.User.query.get(normal_user.id)
    assert (user.subscription_expires_at - datetime.utcnow()).days < 400


def test_replayed_upgrade_does_not_chain_twice(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Replay Plan", duration_value=12)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_W2_REPLAY")

    first = app_module.activate_payment(order)
    assert first["replay"] is False
    end_after_first = app_module.User.query.get(normal_user.id).subscription_expires_at

    replay = app_module.activate_payment(app_module.PaymentOrder.query.get(order.id))
    assert replay["replay"] is True
    assert app_module.User.query.get(normal_user.id).subscription_expires_at == end_after_first


def test_upgrade_preserves_usage_counters_and_addons(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 90, source_id=7108)
    _ledger(app_module, db_session, normal_user, "PROJECT_CAPACITY", 3, source_id=7109)
    normal_user.scans_used = 11
    normal_user.projects_used = 2
    db_session.commit()

    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, plan, "ORD_W2_KEEP"))

    user = app_module.User.query.get(normal_user.id)
    assert user.scans_used == 11 and user.projects_used == 2
    assert user.subscribed_scan_limit == plan.total_scan_limit + 90
    assert user.subscribed_project_limit == plan.total_project_limit + 3


# ===========================================================================
# Downgrade: deferred to the next term boundary, never destructive
# ===========================================================================
def _downgrade_pair(app_module, db_session):
    high = _new_plan(app_module, db_session, "High Tier", total_project_limit=20, total_scan_limit=2000,
                     max_pairs_per_project=8, duration_value=12)
    low = _new_plan(app_module, db_session, "Low Tier", total_project_limit=2, total_scan_limit=100,
                    max_pairs_per_project=2, duration_value=12)
    return high, low


def test_is_downgrade_detection(app_module, db_session):
    high, low = _downgrade_pair(app_module, db_session)
    assert ent.is_downgrade(high, low) is True
    assert ent.is_downgrade(low, high) is False
    assert ent.is_downgrade(high, high) is False
    assert ent.is_downgrade(None, low) is False


def test_losing_an_experience_entitlement_counts_as_a_downgrade(app_module, db_session):
    full = _new_plan(app_module, db_session, "Full Modes")
    fewer = _new_plan(app_module, db_session, "Fewer Modes", allow_detect_once=False)
    assert ent.is_downgrade(full, fewer) is True


def test_unlimited_to_finite_is_a_downgrade(app_module, db_session):
    unlimited = _new_plan(app_module, db_session, "Unlimited Scans", total_scan_limit=0)
    finite = _new_plan(app_module, db_session, "Finite Scans", total_scan_limit=100)
    assert ent.is_downgrade(unlimited, finite) is True
    assert ent.is_downgrade(finite, unlimited) is False


def test_downgrade_does_not_apply_mid_term(app_module, db_session, normal_user):
    high, low = _downgrade_pair(app_module, db_session)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_A"))
    user = app_module.User.query.get(normal_user.id)
    high_end = user.subscription_expires_at
    high_scans = user.subscribed_scan_limit

    result = app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_B"))
    assert result["success"] is True and result.get("deferred") is True

    user = app_module.User.query.get(normal_user.id)
    # Still on the higher plan, with the higher allowances, until the term ends.
    assert user.subscription_id == high.id
    assert user.subscribed_scan_limit == high_scans
    assert user.subscription_expires_at == high_end
    assert user.pending_plan_id == low.id
    assert user.pending_plan_effective_at == high_end
    assert app_module.PaymentOrder.query.filter_by(order_id="ORD_W2_DG_B").first().is_deferred_plan_change is True


def test_downgrade_becomes_effective_at_the_term_boundary(app_module, db_session, normal_user):
    high, low = _downgrade_pair(app_module, db_session)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_C"))
    user = app_module.User.query.get(normal_user.id)
    app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_D"))

    # Reach the boundary.
    user = app_module.User.query.get(normal_user.id)
    user.pending_plan_effective_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert app_module.apply_pending_plan_change_if_due(user) is True
    user = app_module.User.query.get(normal_user.id)
    assert user.subscription_id == low.id
    assert user.subscribed_scan_limit == low.total_scan_limit
    assert user.subscribed_project_limit == low.total_project_limit
    assert user.pending_plan_id is None


def test_pending_downgrade_is_not_applied_before_its_boundary(app_module, db_session, normal_user):
    high, low = _downgrade_pair(app_module, db_session)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_E"))
    user = app_module.User.query.get(normal_user.id)
    app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_F"))

    user = app_module.User.query.get(normal_user.id)
    assert app_module.apply_pending_plan_change_if_due(user) is False
    assert user.subscription_id == high.id


def test_downgrade_preserves_projects_pairs_and_purchased_entitlement(app_module, db_session, normal_user):
    """Nothing is ever deleted because the user is over entitlement."""
    high, low = _downgrade_pair(app_module, db_session)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 400, source_id=7110)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_G"))
    user = app_module.User.query.get(normal_user.id)
    project = _project_with_pairs(app_module, db_session, user, 6)
    project.playback_mode = "detect_once"
    db_session.commit()

    app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_H"))
    user = app_module.User.query.get(normal_user.id)
    user.pending_plan_effective_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    app_module.apply_pending_plan_change_if_due(user)

    # Projects, pairs and premium playback mode all survive the downgrade.
    survivor = app_module.Project.query.get(project.id)
    assert survivor is not None
    assert survivor.playback_mode == "detect_once"
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 6
    # Purchased entitlement is not plan policy - it survives too.
    user = app_module.User.query.get(normal_user.id)
    assert user.subscribed_scan_limit == low.total_scan_limit + 400


def test_new_actions_respect_the_lower_policy_once_effective(app_module, db_session, normal_user):
    high, low = _downgrade_pair(app_module, db_session)
    low.allow_detect_once = False
    db_session.commit()
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_I"))
    user = app_module.User.query.get(normal_user.id)
    project = _project_with_pairs(app_module, db_session, user, 6)

    app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_J"))
    user = app_module.User.query.get(normal_user.id)
    user.pending_plan_effective_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    app_module.apply_pending_plan_change_if_due(user)
    user = app_module.User.query.get(normal_user.id)

    # New pair on the already-over-limit project: blocked, nothing deleted.
    ok, _err = app_module._reserve_pair_slots_for_project(
        project.id, 1, app_module.get_plan_pairs_limit(user)
    )
    assert ok is False
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 6
    # Creating into the now-unentitled mode: blocked.
    with app_module.app.test_request_context():
        with pytest.raises(ValueError):
            app_module._resolve_project_experience_playback("image_video", "detect_once", user=user)


def test_upgrade_clears_a_parked_downgrade(app_module, db_session, normal_user):
    high, low = _downgrade_pair(app_module, db_session)
    app_module.activate_payment(_pending_order(app_module, db_session, normal_user, high, "ORD_W2_DG_K"))
    user = app_module.User.query.get(normal_user.id)
    app_module.activate_payment(_pending_order(app_module, db_session, user, low, "ORD_W2_DG_L"))
    assert app_module.User.query.get(normal_user.id).pending_plan_id == low.id

    even_higher = _new_plan(app_module, db_session, "Higher Tier", total_project_limit=50,
                            total_scan_limit=5000, max_pairs_per_project=9, duration_value=12)
    app_module.activate_payment(
        _pending_order(app_module, db_session, app_module.User.query.get(normal_user.id), even_higher, "ORD_W2_DG_M")
    )
    user = app_module.User.query.get(normal_user.id)
    assert user.pending_plan_id is None
    assert user.subscription_id == even_higher.id


# ===========================================================================
# Coverage vs entitlement must stay conceptually separate
# ===========================================================================
def test_resolver_reports_no_project_service_coverage(app_module, db_session, normal_user):
    """Entitlement = what the ACCOUNT may do. Coverage = whether a PROJECT is
    still paid for. They must never be conflated."""
    e = _ents(app_module, normal_user)
    assert not any("coverage" in key.lower() for key in e)
