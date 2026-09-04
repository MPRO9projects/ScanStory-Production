"""Issue 3E-A: multi-video-per-target commercial entitlement foundation.

Covers only the plan-level commercial contract established in this phase:
SubscriptionPlan.allow_multi_video_per_target / max_videos_per_target, the
entitlements.py resolver fields built on top of them, the immutable server
ceiling, admin plan-form parsing/validation, and plan_revision auditability.

Deliberately NOT covered here (does not exist yet): PairMedia, add-video
enforcement, Creator multi-video UI, scanner media chooser, account/addon
overrides. Those are later phases.
"""
import pytest

import entitlements as ent


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


def _ents(app_module, user):
    return app_module.user_entitlements(user)


def _plan_form(**overrides):
    data = {
        "plan_name": "Issue3EA Plan",
        "plan_description": "Issue 3E-A plan",
        "currency": "INR",
        "plan_amount": "999",
        "offer_price": "899",
        "duration_type": "time",
        "duration_value": "12",
        "trial_days": "0",
        "total_project_limit": "5",
        "total_scan_limit": "500",
        "max_pairs_per_project": "10",
        "display_order": "1",
        "plan_family": "INDIVIDUAL",
        "lifecycle_status": "ACTIVE",
        "plan_flags_form": "1",
        "is_active": "on",
        "plan_experience_form": "1",
        "allow_tracked_overlay": "on",
        "allow_detect_once": "on",
        "allow_direct_qr": "on",
        "plan_media_form": "1",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


# ===========================================================================
# 1. Existing plans / model default (migration-level backfill is covered in
#    tests/migrations/test_multi_video_entitlement_migration.py; this proves
#    the ORM-level default the test-suite's db.create_all() path relies on).
# ===========================================================================
def test_new_plan_defaults_multi_video_disabled(app_module, db_session):
    plan = _new_plan(app_module, db_session, "No Media Overrides")
    assert plan.allow_multi_video_per_target is False
    assert plan.max_videos_per_target is None


# ===========================================================================
# 2-3. New plan can enable multi-video; the max persists.
# ===========================================================================
def test_plan_can_enable_multi_video_with_a_max(app_module, db_session):
    plan = _new_plan(
        app_module, db_session, "Multi Video Plan",
        allow_multi_video_per_target=True, max_videos_per_target=5,
    )
    db_session.expire_all()
    refreshed = app_module.SubscriptionPlan.query.get(plan.id)
    assert refreshed.allow_multi_video_per_target is True
    assert refreshed.max_videos_per_target == 5


# ===========================================================================
# 6-7. Resolver: disabled -> effective max is exactly 1, never unlimited.
#      Enabled -> plan's configured max is honored.
# ===========================================================================
def test_disabled_plan_resolves_effective_max_to_one(app_module, db_session, normal_user):
    plan = _new_plan(app_module, db_session, "Disabled Multi Video")
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["allow_multi_video_per_target"] is False
    assert e["effective_max_videos_per_target"] == 1


def test_enabled_plan_resolves_configured_max(app_module, db_session, normal_user):
    plan = _new_plan(
        app_module, db_session, "Enabled Multi Video",
        allow_multi_video_per_target=True, max_videos_per_target=4,
    )
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["allow_multi_video_per_target"] is True
    assert e["max_videos_per_target"] == 4
    assert e["effective_max_videos_per_target"] == 4


# ===========================================================================
# 8. Server ceiling caps the plan value - an admin editing the DB row
#    directly (bypassing form validation) must still never exceed it via the
#    resolver.
# ===========================================================================
def test_server_ceiling_caps_plan_value_in_resolver(app_module, db_session, normal_user):
    plan = _new_plan(
        app_module, db_session, "Greedy Multi Video",
        allow_multi_video_per_target=True,
        max_videos_per_target=ent.MAX_VIDEOS_PER_TARGET_CEILING * 100,
    )
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["effective_max_videos_per_target"] == ent.MAX_VIDEOS_PER_TARGET_CEILING


# ===========================================================================
# 4-5. Admin form validation: enabled + max < 2 rejected; enabled + max over
#      the server ceiling rejected.
# ===========================================================================
def test_form_rejects_enabled_with_max_below_two(app_module):
    values, error = app_module._plan_form_values(_plan_form(
        allow_multi_video_per_target="on", max_videos_per_target="1",
    ))
    assert values is None
    assert "at least 2" in error


def test_form_rejects_enabled_with_max_over_server_ceiling(app_module):
    values, error = app_module._plan_form_values(_plan_form(
        allow_multi_video_per_target="on",
        max_videos_per_target=str(ent.MAX_VIDEOS_PER_TARGET_CEILING + 1),
    ))
    assert values is None
    assert "cannot exceed" in error


def test_form_rejects_enabled_with_no_max_supplied(app_module):
    values, error = app_module._plan_form_values(_plan_form(
        allow_multi_video_per_target="on", max_videos_per_target="",
    ))
    assert values is None
    assert "required" in error


def test_form_accepts_enabled_with_valid_max(app_module):
    values, error = app_module._plan_form_values(_plan_form(
        allow_multi_video_per_target="on", max_videos_per_target="3",
    ))
    assert error is None
    assert values["allow_multi_video_per_target"] is True
    assert values["max_videos_per_target"] == 3


def test_form_disabled_normalizes_max_to_none(app_module):
    """Documented Step-5 choice: disabling the feature normalizes the stored
    max to None rather than leaving a stale number in place."""
    values, error = app_module._plan_form_values(_plan_form(
        allow_multi_video_per_target=None, max_videos_per_target="7",
    ))
    assert error is None
    assert values["allow_multi_video_per_target"] is False
    assert values["max_videos_per_target"] is None


def test_form_without_media_marker_leaves_existing_values_untouched(app_module, db_session):
    """An older/partial form submission that never renders the media section
    at all must not blank an already-configured plan's setting."""
    plan = _new_plan(
        app_module, db_session, "Preexisting Multi Video",
        allow_multi_video_per_target=True, max_videos_per_target=6,
    )
    form = _plan_form()
    del form["plan_media_form"]
    values, error = app_module._plan_form_values(form, existing=plan)
    assert error is None
    assert "allow_multi_video_per_target" not in values
    assert "max_videos_per_target" not in values


# ===========================================================================
# 9-10. Admin add/edit routes actually persist the new fields end to end.
# ===========================================================================
def test_admin_add_plan_route_persists_multi_video_fields(client, app_module, db_session, admin):
    _login_admin(client, admin)
    before = app_module.SubscriptionPlan.query.count()
    client.post("/admin/plans/add", data=_plan_form(
        plan_name="Route Added Plan",
        allow_multi_video_per_target="on", max_videos_per_target="4",
    ))
    assert app_module.SubscriptionPlan.query.count() == before + 1
    plan = app_module.SubscriptionPlan.query.filter_by(plan_name="Route Added Plan").one()
    assert plan.allow_multi_video_per_target is True
    assert plan.max_videos_per_target == 4


def test_admin_edit_plan_route_persists_multi_video_fields(client, app_module, db_session, admin):
    _login_admin(client, admin)
    plan = _new_plan(app_module, db_session, "Route Edited Plan")
    client.post(f"/admin/plans/{plan.id}/edit", data=_plan_form(
        plan_name="Route Edited Plan",
        allow_multi_video_per_target="on", max_videos_per_target="5",
    ))
    db_session.refresh(plan)
    assert plan.allow_multi_video_per_target is True
    assert plan.max_videos_per_target == 5


def test_admin_edit_plan_route_rejects_invalid_max_and_persists_nothing(client, app_module, db_session, admin):
    _login_admin(client, admin)
    plan = _new_plan(app_module, db_session, "Route Rejected Edit")
    client.post(f"/admin/plans/{plan.id}/edit", data=_plan_form(
        plan_name="Route Rejected Edit",
        allow_multi_video_per_target="on", max_videos_per_target="1",
    ))
    db_session.refresh(plan)
    assert plan.allow_multi_video_per_target is False
    assert plan.max_videos_per_target is None


# ===========================================================================
# 11-12. plan_revision auditability: both new fields are tracked.
# ===========================================================================
# These three isolate causation to the media fields specifically by driving
# _apply_plan_values() directly with only those two keys - the same function
# and the same before/bump logic admin_edit_plan() itself uses (mirrored,
# not reimplemented). Routing these through the full HTTP form instead would
# also change plan_amount/max_pairs_per_project/etc. to _plan_form()'s
# defaults, which are themselves tracked fields - a revision bump would then
# be real but for the WRONG reason, and a no-op test would be unable to tell
# a passing case from one where the media wiring is silently broken.
def test_enabling_multi_video_bumps_plan_revision(app_module, db_session):
    plan = _new_plan(app_module, db_session, "Revision Flag Plan")
    assert plan.plan_revision == 1
    if app_module._apply_plan_values(plan, {"allow_multi_video_per_target": True, "max_videos_per_target": 3}):
        plan.plan_revision = int(plan.plan_revision or 1) + 1
    db_session.commit()
    assert plan.plan_revision == 2


def test_changing_max_videos_bumps_plan_revision(app_module, db_session):
    plan = _new_plan(
        app_module, db_session, "Revision Max Plan",
        allow_multi_video_per_target=True, max_videos_per_target=3,
    )
    assert plan.plan_revision == 1
    if app_module._apply_plan_values(plan, {"allow_multi_video_per_target": True, "max_videos_per_target": 4}):
        plan.plan_revision = int(plan.plan_revision or 1) + 1
    db_session.commit()
    assert plan.max_videos_per_target == 4
    assert plan.plan_revision == 2


def test_no_op_media_edit_does_not_bump_revision(app_module, db_session):
    """Resubmitting the exact same media policy is not a commercial change."""
    plan = _new_plan(
        app_module, db_session, "Revision Stable Plan",
        allow_multi_video_per_target=True, max_videos_per_target=3,
    )
    assert plan.plan_revision == 1
    if app_module._apply_plan_values(plan, {"allow_multi_video_per_target": True, "max_videos_per_target": 3}):
        plan.plan_revision = int(plan.plan_revision or 1) + 1
    db_session.commit()
    assert plan.plan_revision == 1


def test_policy_snapshot_includes_multi_video_fields(app_module, db_session):
    plan = _new_plan(
        app_module, db_session, "Snapshot Multi Video",
        allow_multi_video_per_target=True, max_videos_per_target=5,
    )
    snapshot = plan.policy_snapshot()
    assert snapshot["allow_multi_video_per_target"] is True
    assert snapshot["max_videos_per_target"] == 5


# ===========================================================================
# 13-14. Existing entitlement fields are completely unaffected.
# ===========================================================================
def test_existing_experience_flags_and_pairs_limit_unaffected(app_module, db_session, normal_user):
    plan = _new_plan(
        app_module, db_session, "Unaffected Plan",
        max_pairs_per_project=7, allow_direct_qr=False,
        allow_multi_video_per_target=True, max_videos_per_target=3,
    )
    normal_user.subscription_id = plan.id
    db_session.commit()
    e = _ents(app_module, normal_user)
    assert e["allow_direct_qr"] is False
    assert e["allow_detect_once"] is True
    assert e["allow_tracked_overlay"] is True
    assert e["max_pairs_per_project"] == 7
    assert app_module.get_plan_pairs_limit(normal_user) == 7


def test_is_downgrade_still_works_for_pre_existing_dimensions(app_module, db_session):
    high = _new_plan(app_module, db_session, "High Unaffected", total_scan_limit=1000)
    low = _new_plan(app_module, db_session, "Low Unaffected", total_scan_limit=100)
    assert ent.is_downgrade(high, low) is True
    assert ent.is_downgrade(low, high) is False


def test_losing_multi_video_entitlement_counts_as_a_downgrade(app_module, db_session):
    with_feature = _new_plan(
        app_module, db_session, "Has Multi Video",
        allow_multi_video_per_target=True, max_videos_per_target=5,
    )
    without_feature = _new_plan(app_module, db_session, "No Multi Video")
    assert ent.is_downgrade(with_feature, without_feature) is True
    assert ent.is_downgrade(without_feature, with_feature) is False


def test_lowering_max_videos_while_still_enabled_counts_as_a_downgrade(app_module, db_session):
    high = _new_plan(
        app_module, db_session, "High Video Cap",
        allow_multi_video_per_target=True, max_videos_per_target=8,
    )
    low = _new_plan(
        app_module, db_session, "Low Video Cap",
        allow_multi_video_per_target=True, max_videos_per_target=3,
    )
    assert ent.is_downgrade(high, low) is True
    assert ent.is_downgrade(low, high) is False


def test_resolver_shape_gains_multi_video_keys_without_losing_existing_ones(app_module, db_session, normal_user):
    e = _ents(app_module, normal_user)
    for key in ("allow_multi_video_per_target", "max_videos_per_target", "effective_max_videos_per_target"):
        assert key in e
    for key in ("max_pairs_per_project", "allow_direct_qr", "allow_detect_once", "allow_tracked_overlay",
                "effective_project_limit", "effective_scan_limit"):
        assert key in e
