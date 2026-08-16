"""Wave 1 regression tests for the nine P0 production blockers.

Each test fails on the pre-fix baseline. Grouped by blocker id so a failure
points straight at the defect it guards.
"""
import os
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _paid_plan(app_module):
    return app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()


def _pending_order(app_module, db_session, user, plan, order_id, rzp_order_id):
    order = app_module.PaymentOrder(
        order_id=order_id,
        razorpay_order_id=rzp_order_id,
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


def _ledger(app_module, db_session, user, entitlement_type, delta, source_id):
    row = app_module.EntitlementTransaction(
        user_id=user.id,
        entitlement_type=entitlement_type,
        delta_value=delta,
        source_type="ADDON_PURCHASE",
        source_id=source_id,
        reason="test fixture",
    )
    db_session.add(row)
    db_session.commit()
    return row


# ===========================================================================
# P0-1 - plan activation must not destroy entitlements or usage counters
# ===========================================================================
def test_p0_1_activation_with_no_addons_sets_plan_limits(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_BASE", "order_p1_base")

    result = app_module.activate_payment(order)

    assert result["success"] is True and result["replay"] is False
    user = app_module.User.query.get(normal_user.id)
    assert user.subscribed_project_limit == plan.total_project_limit
    assert user.subscribed_scan_limit == plan.total_scan_limit
    assert user.subscription_status == "active"


def test_p0_1_purchased_extra_scans_survive_activation(app_module, db_session, normal_user):
    """The core revenue-loss bug: EXTRA_SCANS were annihilated on activation."""
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 750, source_id=9001)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_SCANS", "order_p1_scans")

    app_module.activate_payment(order)

    user = app_module.User.query.get(normal_user.id)
    assert user.subscribed_scan_limit == plan.total_scan_limit + 750


def test_p0_1_purchased_project_capacity_still_survives_activation(app_module, db_session, normal_user):
    """Guards the behaviour that already worked, so the scan fix cannot break it."""
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "PROJECT_CAPACITY", 7, source_id=9002)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_PROJ", "order_p1_proj")

    app_module.activate_payment(order)

    user = app_module.User.query.get(normal_user.id)
    assert user.subscribed_project_limit == plan.total_project_limit + 7


def test_p0_1_existing_usage_counters_are_not_reset(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    normal_user.projects_used = 18
    normal_user.scans_used = 63
    db_session.commit()
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_USAGE", "order_p1_usage")

    app_module.activate_payment(order)

    user = app_module.User.query.get(normal_user.id)
    assert user.projects_used == 18, "real projects must not be forgiven by a purchase"
    assert user.scans_used == 63


def test_p0_1_same_plan_renewal_does_not_reset_counters(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    first = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_R1", "order_p1_r1")
    app_module.activate_payment(first)

    user = app_module.User.query.get(normal_user.id)
    user.projects_used = 4
    user.scans_used = 11
    db_session.commit()

    second = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_R2", "order_p1_r2")
    app_module.activate_payment(second)

    user = app_module.User.query.get(normal_user.id)
    assert user.projects_used == 4
    assert user.scans_used == 11


def test_p0_1_duplicate_activation_is_idempotent(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_IDEM", "order_p1_idem")

    first = app_module.activate_payment(order)
    end_after_first = app_module.PaymentOrder.query.get(order.id).subscription_end

    user = app_module.User.query.get(normal_user.id)
    user.scans_used = 5
    db_session.commit()

    second = app_module.activate_payment(app_module.PaymentOrder.query.get(order.id))

    assert first["replay"] is False
    assert second["success"] is True and second["replay"] is True
    assert app_module.PaymentOrder.query.get(order.id).subscription_end == end_after_first
    assert app_module.User.query.get(normal_user.id).scans_used == 5


def test_p0_1_webhook_replay_is_idempotent(app_module, db_session, normal_user):
    """A webhook delivery after a browser verification must not re-apply anything."""
    plan = _paid_plan(app_module)
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 200, source_id=9003)
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_HOOK", "order_p1_hook")

    app_module.activate_payment(order)
    limit_after_browser = app_module.User.query.get(normal_user.id).subscribed_scan_limit

    replay = app_module.activate_payment(app_module.PaymentOrder.query.get(order.id))

    assert replay["replay"] is True
    assert app_module.User.query.get(normal_user.id).subscribed_scan_limit == limit_after_browser
    assert limit_after_browser == plan.total_scan_limit + 200


def test_p0_1_browser_and_webhook_share_one_activation_path():
    """Structural: neither route may re-implement activation."""
    source = open("app.py", encoding="utf-8", errors="ignore").read()

    verify_start = source.index("def verify_payment(")
    verify_body = source[verify_start:verify_start + 6000]
    assert "activate_payment(" in verify_body

    webhook_start = source.index("def razorpay_webhook(")
    webhook_body = source[webhook_start:webhook_start + 9000]
    assert "activate_payment(" in webhook_body

    # Exactly one function assigns the materialised entitlement columns.
    assert source.count("user.subscribed_scan_limit = reconciled_scan_limit(") == 1
    assert source.count("user.subscribed_project_limit = reconciled_project_limit(") == 1


def test_p0_1_reconciled_scan_limit_mirrors_project_reconciler(app_module, db_session, normal_user):
    _ledger(app_module, db_session, normal_user, "EXTRA_SCANS", 120, source_id=9004)
    assert app_module.purchased_scan_capacity(normal_user) == 120
    assert app_module.reconciled_scan_limit(normal_user, 500) == 620
    # None / 0 keep meaning "unlimited", same convention as projects.
    assert app_module.reconciled_scan_limit(normal_user, None) is None
    assert app_module.reconciled_scan_limit(normal_user, 0) == 0


def test_p0_1_time_plan_uses_calendar_months_not_thirty_day_approximation(app_module, db_session, normal_user):
    plan = _paid_plan(app_module)
    plan.duration_type = "time"
    plan.duration_value = 12
    db_session.commit()
    order = _pending_order(app_module, db_session, normal_user, plan, "ORD_P1_CAL", "order_p1_cal")

    app_module.activate_payment(order)

    refreshed = app_module.PaymentOrder.query.get(order.id)
    span = (refreshed.subscription_end - refreshed.subscription_start).days
    assert span >= 365, f"a 12-month plan granted {span} days (the *30 bug grants 360)"


def test_p0_1_add_calendar_months_clamps_month_end(app_module):
    assert app_module._add_calendar_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert app_module._add_calendar_months(datetime(2024, 1, 31), 1) == datetime(2024, 2, 29)
    assert app_module._add_calendar_months(datetime(2026, 6, 15), 12) == datetime(2027, 6, 15)
    assert app_module._add_calendar_months(datetime(2026, 6, 15), 0) == datetime(2026, 6, 15)


# ===========================================================================
# P0-2 - PROJECT_SERVICE_COVERAGE must be insertable
# ===========================================================================
def test_p0_2_project_service_coverage_catalog_row_can_be_created(app_module, db_session):
    item = app_module.AddonCatalog(
        code="coverage-12m",
        name="12 month project coverage",
        addon_type="PROJECT_SERVICE_COVERAGE",
        unit_amount=999.0,
        currency="INR",
        validity_days_delta=365,
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    assert app_module.AddonCatalog.query.filter_by(code="coverage-12m").first() is not None


def test_p0_2_invalid_addon_type_is_still_rejected(app_module, db_session):
    with pytest.raises(Exception):
        app_module.AddonCatalog(
            code="bogus",
            name="bogus",
            # ACCOUNT_STORAGE became a real type in Wave 3, so this needs a
            # genuinely unsupported value to keep testing what it means to.
            addon_type="TELEPORTATION_CREDITS",
            unit_amount=1.0,
        )


def test_p0_2_model_declares_the_same_check_as_the_migration():
    """Model/migration parity is the systemic fix; drift is what hid this bug."""
    import models

    names = {c.name for c in models.AddonCatalog.__table__.constraints if getattr(c, "name", None)}
    assert "ck_addon_catalog_type" in names

    migration = open(
        "migrations/versions/c3f7a1d5e9b4_addon_catalog_type_check_allows_coverage.py",
        encoding="utf-8",
    ).read()
    assert "PROJECT_SERVICE_COVERAGE" in migration
    # The historical revision must not have been edited.
    original = open(
        "migrations/versions/f4a8c2b91d70_addon_entitlement_foundation.py", encoding="utf-8"
    ).read()
    assert "'EXTRA_SCANS', 'VALIDITY_EXTENSION', 'PROJECT_CAPACITY')" in original


# ===========================================================================
# P0-3 - add-on catalogue must be operable without raw DB access
# ===========================================================================
def _superadmin_login(client, app_module, db_session, email="super@example.com"):
    admin = app_module.Admin(
        email=email,
        name="Super",
        password_hash=generate_password_hash("password123"),
        role="superadmin",
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    return admin


def _plain_admin_login(client, app_module, db_session, email="plain@example.com"):
    admin = app_module.Admin(
        email=email,
        name="Plain",
        password_hash=generate_password_hash("password123"),
        role="admin",
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    return admin


def _addon_form(**overrides):
    form = {
        "code": "extra-scans-500",
        "name": "500 extra scans",
        "addon_type": "EXTRA_SCANS",
        "unit_amount": "499.00",
        "currency": "INR",
        "scan_delta": "500",
        "is_active": "on",
        "is_commercially_available": "on",
    }
    form.update(overrides)
    return form


def test_p0_3_superadmin_can_create_and_edit_catalog_items(client, app_module, db_session):
    _superadmin_login(client, app_module, db_session)

    response = client.post("/admin/addons/create", data=_addon_form(), follow_redirects=False)
    assert response.status_code == 302

    item = app_module.AddonCatalog.query.filter_by(code="extra-scans-500").first()
    assert item is not None and item.scan_delta == 500

    response = client.post(
        f"/admin/addons/{item.id}/edit",
        data=_addon_form(name="500 extra scans (v2)", unit_amount="599.00"),
    )
    assert response.status_code == 302
    refreshed = app_module.AddonCatalog.query.get(item.id)
    assert refreshed.name == "500 extra scans (v2)" and refreshed.unit_amount == 599.0


def test_p0_3_catalog_actions_are_audit_logged(client, app_module, db_session):
    admin = _superadmin_login(client, app_module, db_session)
    client.post("/admin/addons/create", data=_addon_form())

    activity = app_module.AdminActivity.query.filter_by(
        admin_id=admin.id, activity_type="addon_create"
    ).first()
    assert activity is not None


def test_p0_3_plain_admin_is_denied(client, app_module, db_session):
    _plain_admin_login(client, app_module, db_session)

    listing = client.get("/admin/addons")
    created = client.post("/admin/addons/create", data=_addon_form(code="denied-item"))

    assert listing.status_code == 302
    assert created.status_code == 302
    assert app_module.AddonCatalog.query.filter_by(code="denied-item").first() is None


def test_p0_3_no_destructive_delete_route_exists(app_module):
    rules = {r.rule for r in app_module.app.url_map.iter_rules()}
    assert "/admin/addons/<int:catalog_id>/delete" not in rules


def test_p0_3_referenced_item_can_be_deactivated_not_destroyed(client, app_module, db_session, normal_user):
    _superadmin_login(client, app_module, db_session)
    client.post("/admin/addons/create", data=_addon_form())
    item = app_module.AddonCatalog.query.filter_by(code="extra-scans-500").first()

    purchase = app_module.AddonPurchase(
        order_id="ADDON_ORD_1",
        user_id=normal_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency="INR",
        status="fulfilled",
    )
    db_session.add(purchase)
    db_session.commit()

    response = client.post(f"/admin/addons/{item.id}/toggle", data={"field": "is_active"})

    assert response.status_code == 302
    refreshed = app_module.AddonCatalog.query.get(item.id)
    assert refreshed is not None, "catalog rows referenced by purchases must survive"
    assert refreshed.is_active is False
    assert app_module.AddonPurchase.query.get(purchase.id).catalog_id == item.id


def test_p0_3_inactive_items_are_excluded_from_the_commercial_api(client, app_module, db_session, normal_user):
    live = app_module.AddonCatalog(
        code="live-scans", name="Live", addon_type="EXTRA_SCANS",
        unit_amount=100.0, currency="INR", scan_delta=100,
        is_active=True, is_commercially_available=True,
    )
    hidden = app_module.AddonCatalog(
        code="hidden-scans", name="Hidden", addon_type="EXTRA_SCANS",
        unit_amount=100.0, currency="INR", scan_delta=100,
        is_active=False, is_commercially_available=True,
    )
    unlisted = app_module.AddonCatalog(
        code="unlisted-scans", name="Unlisted", addon_type="EXTRA_SCANS",
        unit_amount=100.0, currency="INR", scan_delta=100,
        is_active=True, is_commercially_available=False,
    )
    db_session.add_all([live, hidden, unlisted])
    db_session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id
    payload = client.get("/api/addons/catalog").get_json()

    codes = {a["code"] for a in payload["addons"]}
    assert codes == {"live-scans"}


def test_p0_3_seeding_is_idempotent(app_module, db_session):
    entries = [{
        "code": "seeded-capacity-5",
        "name": "5 extra projects",
        "addon_type": "PROJECT_CAPACITY",
        "unit_amount": 250.0,
        "project_delta": 5,
    }]

    created_1, updated_1 = app_module.seed_addon_catalog_items(entries)
    created_2, updated_2 = app_module.seed_addon_catalog_items(entries)

    assert (created_1, updated_1) == (1, 0)
    assert (created_2, updated_2) == (0, 1)
    assert app_module.AddonCatalog.query.filter_by(code="seeded-capacity-5").count() == 1


def test_p0_3_seeding_accepts_project_service_coverage(app_module, db_session):
    """Depends on P0-2: the type must be insertable for the seed to work."""
    created, _ = app_module.seed_addon_catalog_items([{
        "code": "seeded-coverage",
        "name": "Project coverage 1y",
        "addon_type": "PROJECT_SERVICE_COVERAGE",
        "unit_amount": 999.0,
        "validity_days_delta": 365,
    }])
    assert created == 1


def test_p0_3_seed_cli_refuses_to_invent_prices(app_module, monkeypatch):
    monkeypatch.delenv("ADDON_CATALOG_SEED_FILE", raising=False)
    monkeypatch.delenv("ADDON_CATALOG_SEED_JSON", raising=False)

    result = app_module.app.test_cli_runner().invoke(args=["seed-addon-catalog", "--apply"])

    assert result.exit_code != 0
    assert "never defaulted" in result.output
    assert app_module.AddonCatalog.query.count() == 0


def test_p0_3_seed_cli_is_dry_run_by_default(app_module, monkeypatch, tmp_path):
    source = tmp_path / "addons.json"
    source.write_text(
        '[{"code": "cli-scans", "name": "CLI", "addon_type": "EXTRA_SCANS", '
        '"unit_amount": 100.0, "scan_delta": 100}]',
        encoding="utf-8",
    )

    dry = app_module.app.test_cli_runner().invoke(
        args=["seed-addon-catalog", "--file", str(source)]
    )
    assert dry.exit_code == 0 and "Dry run" in dry.output
    assert app_module.AddonCatalog.query.filter_by(code="cli-scans").first() is None

    applied = app_module.app.test_cli_runner().invoke(
        args=["seed-addon-catalog", "--file", str(source), "--apply"]
    )
    assert applied.exit_code == 0
    assert app_module.AddonCatalog.query.filter_by(code="cli-scans").first() is not None


# ===========================================================================
# P0-4 - project deletion must remove the right files, from the right place
# ===========================================================================
def _make_project(app_module, db_session, *, owner_user=None, owner_admin=None, pair_index=1):
    project = app_module.Project(
        name="Delete me",
        owner_user_id=owner_user.id if owner_user else None,
        current_owner_user_id=owner_user.id if owner_user else None,
        owner_admin_id=owner_admin.id if owner_admin else None,
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=pair_index,
        image_filename=f"{project.id}_{pair_index}.jpg",
        video_filename=f"{project.id}_{pair_index}.mp4",
    )
    db_session.add(pair)
    project.qr_code_path = f"/qr/project_{project.id}_main.png"
    project.qr_code_filename = f"project_{project.id}_main.png"
    db_session.commit()
    return project, pair


def _write_media(app_module, project, pair):
    images, videos, features, qr = app_module.project_media_dirs(project)
    paths = [
        os.path.join(images, pair.image_filename),
        os.path.join(videos, pair.video_filename),
        os.path.join(features, f"{project.id}_{pair.pair_index}.npz"),
        os.path.join(qr, project.qr_code_filename),
    ]
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x")
    return paths


def test_p0_4_user_project_delete_removes_exactly_its_files(app_module, db_session, normal_user):
    project, pair = _make_project(app_module, db_session, owner_user=normal_user)
    paths = _write_media(app_module, project, pair)

    failures = app_module._delete_project_files_and_rows(project)

    assert failures == []
    assert [p for p in paths if os.path.exists(p)] == []


def test_p0_4_admin_project_delete_removes_its_files(app_module, db_session):
    """The actual fix: admin projects live in data_admin/ and were never touched."""
    admin = app_module.Admin.query.first()
    project, pair = _make_project(app_module, db_session, owner_admin=admin)
    paths = _write_media(app_module, project, pair)

    # Prove the fixture really wrote into the admin tree, not the user tree.
    assert all(app_module.ADMIN_DATA_DIR in os.path.abspath(p) for p in paths)

    failures = app_module._delete_project_files_and_rows(project)

    assert failures == []
    assert [p for p in paths if os.path.exists(p)] == []


def test_p0_4_media_dirs_follow_ownership_not_the_caller(app_module, db_session, normal_user):
    admin = app_module.Admin.query.first()
    user_project, _ = _make_project(app_module, db_session, owner_user=normal_user)
    admin_project, _ = _make_project(app_module, db_session, owner_admin=admin)

    assert app_module.project_media_dirs(user_project)[0] == app_module.IMAGES_DIR
    assert app_module.project_media_dirs(admin_project)[0] == app_module.ADMIN_IMAGES_DIR


def test_p0_4_unrelated_project_files_are_untouched(app_module, db_session, normal_user):
    keep_project, keep_pair = _make_project(app_module, db_session, owner_user=normal_user, pair_index=1)
    keep_paths = _write_media(app_module, keep_project, keep_pair)

    doomed, doomed_pair = _make_project(app_module, db_session, owner_user=normal_user, pair_index=1)
    _write_media(app_module, doomed, doomed_pair)

    app_module._delete_project_files_and_rows(doomed)

    assert all(os.path.exists(p) for p in keep_paths)


def test_p0_4_missing_files_are_safe(app_module, db_session, normal_user):
    project, _pair = _make_project(app_module, db_session, owner_user=normal_user)
    # Deliberately write nothing.
    failures = app_module._delete_project_files_and_rows(project)
    assert failures == []


def test_p0_4_unlink_failure_is_surfaced_and_logged(app_module, db_session, normal_user, monkeypatch, caplog):
    project, pair = _make_project(app_module, db_session, owner_user=normal_user)
    paths = _write_media(app_module, project, pair)
    blocked = paths[0]

    real_remove = os.remove

    def flaky_remove(path):
        if os.path.abspath(path) == os.path.abspath(blocked):
            raise PermissionError("file is locked")
        return real_remove(path)

    monkeypatch.setattr(app_module.os, "remove", flaky_remove)

    with caplog.at_level("ERROR"):
        failures = app_module._delete_project_files_and_rows(project)

    assert len(failures) == 1, "an unlink failure must not be swallowed"
    assert "project_media_unlink_failed" in caplog.text
    assert "project_delete_incomplete_media_cleanup" in caplog.text


def test_p0_4_delete_response_leaks_no_absolute_paths(client, app_module, db_session, normal_user):
    project, pair = _make_project(app_module, db_session, owner_user=normal_user)
    _write_media(app_module, project, pair)
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.post(f"/projects/{project.id}/delete", follow_redirects=True)

    body = response.get_data(as_text=True)
    assert app_module.DATA_DIR.replace("\\", "/") not in body.replace("\\", "/")
    assert "Traceback" not in body


def test_p0_4_stored_filename_cannot_escape_the_media_root(app_module):
    root = app_module.IMAGES_DIR
    assert app_module._safe_media_path(root, "../../etc/passwd") == os.path.abspath(
        os.path.join(root, "passwd")
    )
    assert app_module._safe_media_path(root, "..") is None
    assert app_module._safe_media_path(root, "") is None
    assert app_module._safe_media_path(root, "a/b/c.jpg") == os.path.abspath(
        os.path.join(root, "c.jpg")
    )


# ===========================================================================
# P0-5 - resumable sessions must not block project deletion
# ===========================================================================
def _upload_session(app_module, db_session, user, project, pair):
    session_row = app_module.UploadSession(
        owner_user_id=user.id,
        image_size=4,
        video_size=6,
        expected_total_size=10,
        current_offset=10,
        status="completed",
        storage_token="tok-" + str(project.id),
        project_id=project.id,
        pair_id=pair.id,
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    db_session.add(session_row)
    db_session.commit()
    return session_row


def test_p0_5_deleting_a_resumably_uploaded_project_succeeds(app_module, db_session, normal_user):
    project, pair = _make_project(app_module, db_session, owner_user=normal_user)
    session_row = _upload_session(app_module, db_session, normal_user, project, pair)

    app_module._delete_project_files_and_rows(project)

    assert app_module.Project.query.get(project.id) is None
    surviving = app_module.UploadSession.query.get(session_row.id)
    assert surviving is not None, "upload sessions are retained audit history"
    assert surviving.project_id is None
    assert surviving.pair_id is None


def test_p0_5_retention_policy_is_declared_in_schema_not_only_in_the_helper():
    """ON DELETE SET NULL covers the ORM-cascade paths that bypass the helper."""
    import models

    for column_name in ("project_id", "pair_id"):
        column = models.UploadSession.__table__.c[column_name]
        ondelete = {fk.ondelete for fk in column.foreign_keys}
        assert ondelete == {"SET NULL"}, f"{column_name} must be ON DELETE SET NULL"


# ===========================================================================
# P0-6 - the queue must fail closed instead of silently becoming a no-op
# ===========================================================================
@pytest.mark.parametrize("mode", ["fake", "inline"])
def test_p0_6_production_rejects_non_rq_queue_modes(monkeypatch, mode):
    import importlib
    import sys

    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", mode)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SCANSTORY_TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x")
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM"):
        monkeypatch.setenv(key, "value")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    sys.modules.pop("app", None)

    with pytest.raises(RuntimeError) as exc:
        importlib.import_module("app")
    assert "SCANSTORY_QUEUE_MODE" in str(exc.value)
    sys.modules.pop("app", None)


def test_p0_6_production_rejects_scanstory_testing(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SCANSTORY_TESTING", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x")
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM"):
        monkeypatch.setenv(key, "value")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    sys.modules.pop("app", None)

    with pytest.raises(RuntimeError) as exc:
        importlib.import_module("app")
    assert "SCANSTORY_TESTING=0" in str(exc.value)
    sys.modules.pop("app", None)


def test_p0_6_production_requires_redis(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SCANSTORY_TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x")
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM"):
        monkeypatch.setenv(key, "value")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    sys.modules.pop("app", None)

    with pytest.raises(RuntimeError) as exc:
        importlib.import_module("app")
    assert "REDIS_URL" in str(exc.value)
    sys.modules.pop("app", None)


def test_p0_6_undeclared_environment_refuses_to_boot(monkeypatch):
    """A deploy that declares no environment used to boot silently into 'fake'."""
    import importlib
    import sys

    for key in ("SCANSTORY_PRODUCTION", "APP_ENV", "ENV", "FLASK_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SCANSTORY_TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x")
    sys.modules.pop("app", None)

    with pytest.raises(RuntimeError) as exc:
        importlib.import_module("app")
    assert "environment is not declared" in str(exc.value)
    sys.modules.pop("app", None)


def test_p0_6_queue_available_fails_closed_in_production(monkeypatch):
    import processing_queue

    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.setenv("SCANSTORY_PRODUCTION", "1")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert processing_queue.queue_available() is False

    monkeypatch.delenv("SCANSTORY_PRODUCTION", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SCANSTORY_QUEUE_REQUIRED", raising=False)
    assert processing_queue.queue_available() is True


def test_p0_6_ready_is_not_green_when_production_runs_a_fake_queue(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "fake")
    monkeypatch.setenv("SCANSTORY_QUEUE_REQUIRED", "1")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.get_json()["checks"]["queue"] == "unavailable"


def test_p0_6_ready_returns_503_when_redis_is_down_in_rq_mode(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: False)

    response = client.get("/ready")

    assert response.status_code == 503


def test_p0_6_ready_is_green_for_a_valid_rq_configuration(client, app_module, monkeypatch):
    monkeypatch.setenv("SCANSTORY_QUEUE_MODE", "rq")
    monkeypatch.setattr(app_module, "redis_ready_check", lambda: True)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.get_json()["checks"]["queue"] == "ok"


def test_p0_6_healthz_stays_a_lightweight_liveness_probe(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


# ===========================================================================
# P0-7 - whole-request ingest must be bounded
# ===========================================================================
def test_p0_7_absolute_cap_is_configured_and_finite(app_module):
    cap = app_module.app.config["MAX_CONTENT_LENGTH"]
    assert isinstance(cap, int) and cap > 0
    # Derived from the real per-file ceilings, so a legitimate multi-pair upload
    # still fits.
    assert cap >= app_module.MAX_VIDEO_SIZE + app_module.MAX_IMAGE_SIZE


def test_p0_7_body_above_the_hard_ceiling_is_rejected(client, app_module):
    oversized = app_module.app.config["MAX_CONTENT_LENGTH"] + 1
    response = client.post(
        "/upload",
        content_type="multipart/form-data; boundary=x",
        environ_overrides={"CONTENT_LENGTH": str(oversized)},
    )
    assert response.status_code == 413


def test_p0_7_non_upload_endpoints_get_a_much_smaller_cap(client, app_module):
    declared = app_module.DEFAULT_MAX_REQUEST_BYTES + 1
    response = client.post(
        "/login/",
        content_type="application/x-www-form-urlencoded",
        environ_overrides={"CONTENT_LENGTH": str(declared)},
    )
    assert response.status_code == 413
    assert response.get_json()["code"] == "REQUEST_TOO_LARGE"


def test_p0_7_multi_pair_upload_endpoints_keep_the_large_allowance(app_module):
    for endpoint in ("handle_upload", "user_edit_project", "admin_handle_upload"):
        assert app_module._endpoint_body_limit(endpoint) == app_module.ABSOLUTE_MAX_REQUEST_BYTES
    assert app_module._endpoint_body_limit("login") < app_module.ABSOLUTE_MAX_REQUEST_BYTES


def test_p0_7_oversized_body_is_rejected_before_any_parsing(client, app_module, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("request body must not be parsed before the size check")

    # Any of these firing proves the body was read/validated before the cap.
    monkeypatch.setattr(app_module, "validate_image", explode)
    monkeypatch.setattr(app_module, "validate_video", explode)

    response = client.post(
        "/upload",
        content_type="multipart/form-data; boundary=x",
        environ_overrides={"CONTENT_LENGTH": str(app_module.app.config["MAX_CONTENT_LENGTH"] + 1)},
    )
    assert response.status_code == 413


def test_p0_7_normal_sized_request_is_unaffected(client):
    response = client.get("/healthz")
    assert response.status_code == 200


def test_p0_7_resumable_chunk_cap_is_still_bounded_and_below_the_default(app_module):
    chunk_cap = app_module.RESUMABLE_UPLOAD_CHUNK_MAX_BYTES
    assert 0 < chunk_cap <= app_module.DEFAULT_MAX_REQUEST_BYTES


# ===========================================================================
# P0-8 - centralized, shareable rate limiting
# ===========================================================================
class FakeRedisPipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def ttl(self, key):
        self._ops.append(("ttl", key))
        return self

    def execute(self):
        results = []
        for op, key in self._ops:
            if op == "incr":
                self._store["counts"][key] = self._store["counts"].get(key, 0) + 1
                results.append(self._store["counts"][key])
            else:
                results.append(self._store["ttls"].get(key, -1))
        self._ops = []
        return results


class FakeRedis:
    """Minimal stand-in for a shared Redis, so two limiter instances can be
    asserted to observe ONE budget (which is the whole point of the fix)."""

    def __init__(self):
        self.store = {"counts": {}, "ttls": {}}

    def pipeline(self):
        return FakeRedisPipeline(self.store)

    def expire(self, key, seconds):
        self.store["ttls"][key] = int(seconds)
        return True


class BrokenRedis(FakeRedis):
    def pipeline(self):
        raise ConnectionError("redis is down")


def test_p0_8_two_worker_like_contexts_share_one_counter():
    import rate_limit

    shared = FakeRedis()
    worker_a = rate_limit.build_limiter(client=shared)
    worker_b = rate_limit.build_limiter(client=shared)

    assert worker_a.check("k", 2, 60)[0] is True
    assert worker_b.check("k", 2, 60)[0] is True
    # Third request across the pair must be denied - a process-local limiter
    # would have allowed 2 per worker, i.e. 4 in total.
    allowed_third, retry_after = worker_a.check("k", 2, 60)
    assert allowed_third is False
    assert retry_after >= 1


def test_p0_8_counter_survives_a_process_restart():
    import rate_limit

    shared = FakeRedis()
    before = rate_limit.build_limiter(client=shared)
    for _ in range(3):
        before.check("restart", 3, 60)

    after_restart = rate_limit.build_limiter(client=shared)
    allowed, _retry = after_restart.check("restart", 3, 60)

    assert allowed is False, "a restart must not hand out a fresh budget"


def test_p0_8_namespaces_are_independent():
    import rate_limit

    shared = FakeRedis()
    limiter = rate_limit.build_limiter(client=shared)

    assert limiter.check("scope_a:1.2.3.4", 1, 60)[0] is True
    assert limiter.check("scope_a:1.2.3.4", 1, 60)[0] is False
    assert limiter.check("scope_b:1.2.3.4", 1, 60)[0] is True


def test_p0_8_redis_unavailable_fails_closed():
    import rate_limit

    limiter = rate_limit.build_limiter(client=BrokenRedis())
    allowed, retry_after = limiter.check("k", 100, 60)

    assert allowed is False, "documented policy for this security control is fail closed"
    assert retry_after == rate_limit.FAIL_CLOSED_RETRY_AFTER


def test_p0_8_testing_fallback_is_the_in_memory_limiter(monkeypatch):
    import rate_limit

    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    limiter = rate_limit.build_limiter()
    assert limiter.backend == "memory"
    assert limiter.shared is False


def test_p0_8_misconfigured_redis_url_is_loud_not_a_silent_downgrade():
    import rate_limit

    with pytest.raises(RuntimeError):
        rate_limit.build_limiter(redis_url="not-a-valid-url://x")


def test_p0_8_identity_digest_never_echoes_the_identifier():
    import rate_limit

    digest = rate_limit.identity_digest("Admin@Example.COM")
    assert "admin@example.com" not in digest.lower()
    assert digest == rate_limit.identity_digest("admin@example.com")
    assert len(digest) == 32


def test_p0_8_admin_login_is_rate_limited(client, app_module, db_session):
    app_module.request_limiter.clear()
    limit, _window = app_module.RATE_LIMITS["admin_login_ip"]

    last = None
    for index in range(limit + 1):
        last = client.post(
            "/admin/login", data={"email": f"admin{index}@example.com", "password": "wrong"}
        )

    assert last.status_code == 429
    assert "Retry-After" in last.headers


def test_p0_8_admin_login_identity_bucket_limits_one_account(client, app_module):
    app_module.request_limiter.clear()
    limit, _window = app_module.RATE_LIMITS["admin_login_identity"]

    last = None
    for _ in range(limit + 1):
        last = client.post("/admin/login", data={"email": "target@example.com", "password": "wrong"})

    assert last.status_code == 429


def test_p0_8_admin_forgot_password_is_rate_limited(client, app_module, db_session):
    app_module.request_limiter.clear()
    admin = app_module.Admin.query.first()
    limit, _window = app_module.RATE_LIMITS["admin_forgot_password_identity"]

    last = None
    for _ in range(limit + 1):
        last = client.post("/admin/forgot-password", data={"email": admin.email})

    assert last.status_code == 429
    assert "Retry-After" in last.headers


def test_p0_8_admin_forgot_password_stops_creating_otps_once_limited(client, app_module, db_session):
    app_module.request_limiter.clear()
    admin = app_module.Admin.query.first()
    limit, _window = app_module.RATE_LIMITS["admin_forgot_password_identity"]

    for _ in range(limit + 4):
        client.post("/admin/forgot-password", data={"email": admin.email})

    created = app_module.OTPCode.query.filter_by(
        email=admin.email, purpose="admin_reset_password"
    ).count()
    assert created <= limit, "mail-bombing must be stopped before an OTP is minted"


def test_p0_8_user_login_still_limited_without_regression(client, app_module, normal_user):
    app_module.request_limiter.clear()
    response = client.post("/login/", data={"email": normal_user.email, "password": "password123"})
    assert response.status_code in (200, 302)


def test_p0_8_webhook_endpoint_is_deliberately_not_rate_limited():
    source = open("app.py", encoding="utf-8", errors="ignore").read()
    start = source.index("def razorpay_webhook(")
    body = source[start:start + 4000]
    assert "_check_rate_limit(" not in body


def test_p0_8_content_report_uses_the_central_limiter():
    source = open("app.py", encoding="utf-8", errors="ignore").read()
    assert '"content_report"' in source
    # No second, parallel limiter mechanism was introduced.
    assert source.count("from rate_limit import") == 1


# ===========================================================================
# P0-9 - admin-owned projects must resolve coverage
# ===========================================================================
def _admin_project_with_media(app_module, db_session):
    admin = app_module.Admin.query.first()
    project, pair = _make_project(app_module, db_session, owner_admin=admin)
    _write_media(app_module, project, pair)
    return project, pair


def test_p0_9_admin_owned_project_is_covered(app_module, db_session):
    project, _pair = _admin_project_with_media(app_module, db_session)

    state = app_module.project_public_access_state(project)

    assert state["is_live"] is True
    assert state["coverage_source"] == "ADMIN_OWNED"


def test_p0_9_admin_media_image_video_and_qr_all_serve(client, app_module, db_session):
    project, pair = _admin_project_with_media(app_module, db_session)

    image = client.get(f"/admin/image/{project.id}/{pair.pair_index}")
    video = client.get(f"/admin/video/{project.id}/{pair.pair_index}")
    qr = client.get(f"/admin/qr/{project.qr_code_filename}")

    assert image.status_code == 200
    assert video.status_code in (200, 206)
    assert qr.status_code == 200


def test_p0_9_admin_media_keeps_private_cache_semantics(client, app_module, db_session):
    project, pair = _admin_project_with_media(app_module, db_session)

    response = client.get(f"/admin/image/{project.id}/{pair.pair_index}")

    cache_control = response.headers.get("Cache-Control", "")
    assert "private" in cache_control
    assert "public" not in cache_control


def test_p0_9_suspended_admin_project_is_still_gated(app_module, db_session):
    project, _pair = _admin_project_with_media(app_module, db_session)
    project.is_active = False
    db_session.commit()

    state = app_module.project_public_access_state(project)

    assert state["is_live"] is False
    assert state["reason"] == "inactive"


def test_p0_9_project_of_a_deactivated_admin_is_not_covered(app_module, db_session):
    project, _pair = _admin_project_with_media(app_module, db_session)
    admin = app_module.Admin.query.get(project.owner_admin_id)
    admin.is_active = False
    db_session.commit()

    assert app_module.project_public_access_state(project)["is_live"] is False


def test_p0_9_user_project_without_coverage_is_still_unavailable(app_module, db_session, normal_user):
    """No coverage bypass may leak into the user-owned invariant."""
    normal_user.subscription_status = "expired"
    normal_user.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()
    project, _pair = _make_project(app_module, db_session, owner_user=normal_user)

    state = app_module.project_public_access_state(project)

    assert state["is_live"] is False
    assert state["reason"] == "no_valid_coverage"


def test_p0_9_admin_created_project_transferred_to_a_user_follows_the_user_rule(
    app_module, db_session, normal_user
):
    """The admin branch must not become a permanent bypass after transfer."""
    admin = app_module.Admin.query.first()
    project, _pair = _make_project(app_module, db_session, owner_admin=admin)
    normal_user.subscription_status = "expired"
    normal_user.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
    project.owner_user_id = normal_user.id
    project.current_owner_user_id = normal_user.id
    db_session.commit()

    assert app_module.project_public_access_state(project)["is_live"] is False
