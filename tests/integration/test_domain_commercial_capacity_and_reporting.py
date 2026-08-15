"""Domain Checkpoint 2B: reusable PAYG project capacity, project-targeted
entitlements, standalone ScanStory service renewal, and the content-report /
admin-moderation backend."""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_user(app_module, db_session, email, *, limit=3, used=0, active=True, expires_in_days=30):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active" if active else "expired",
        subscription_expires_at=(
            datetime.utcnow() + timedelta(days=expires_in_days)
            if active
            else datetime.utcnow() - timedelta(days=1)
        ),
        subscribed_project_limit=limit,
        subscribed_scan_limit=100,
        projects_used=used,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_project(app_module, db_session, owner, *, active=True, name="Coverage Project"):
    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=1,
        scanner_url="/scanner/coverage",
        qr_code_filename="project_coverage_main.png",
        qr_code_path="/qr/project_coverage_main.png",
        is_active=active,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _catalog(app_module, db_session, code, addon_type, **deltas):
    item = app_module.AddonCatalog(
        code=code,
        name=code.replace("_", " ").title(),
        addon_type=addon_type,
        unit_amount=99.0,
        currency="INR",
        scan_delta=deltas.get("scan_delta"),
        validity_days_delta=deltas.get("validity_days_delta"),
        project_delta=deltas.get("project_delta"),
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _purchase(app_module, db_session, user, item, *, project=None, quantity=1, suffix="1"):
    purchase = app_module.AddonPurchase(
        order_id=f"ADDON_TEST_{item.code}_{user.id}_{suffix}",
        user_id=user.id,
        catalog_id=item.id,
        project_id=project.id if project else None,
        quantity=quantity,
        amount=item.unit_amount,
        total_amount=item.unit_amount * quantity,
        currency=item.currency,
        status="pending",
    )
    db_session.add(purchase)
    db_session.commit()
    return purchase


def _login(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _login_admin_role(client, admin_obj):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin_obj.id


# ---------------------------------------------------------------------------
# A. Effective project capacity
# ---------------------------------------------------------------------------
def test_base_capacity_only_has_no_purchased_component(app_module, db_session):
    user = _make_user(app_module, db_session, "base-only@example.com", limit=3)
    assert app_module.purchased_project_capacity(user) == 0
    assert app_module.effective_project_limit(user) == 3
    summary = app_module.project_capacity_summary(user)
    assert summary["base_project_limit"] == 3
    assert summary["projects_remaining"] == 3
    assert summary["unlimited"] is False


@pytest.mark.parametrize("project_delta", [1, 5])
def test_purchased_capacity_raises_effective_limit(app_module, db_session, project_delta):
    user = _make_user(app_module, db_session, f"payg{project_delta}@example.com", limit=3)
    item = _catalog(app_module, db_session, f"CAP_{project_delta}", "PROJECT_CAPACITY", project_delta=project_delta)
    purchase = _purchase(app_module, db_session, user, item)

    result = app_module.fulfill_addon_purchase(purchase)
    assert result["success"] is True
    assert result["delta"] == project_delta

    assert app_module.purchased_project_capacity(user) == project_delta
    assert app_module.effective_project_limit(user) == 3 + project_delta
    assert app_module.project_capacity_summary(user)["base_project_limit"] == 3


def test_multiple_capacity_purchases_accumulate(app_module, db_session):
    user = _make_user(app_module, db_session, "accumulate@example.com", limit=3)
    item = _catalog(app_module, db_session, "CAP_5", "PROJECT_CAPACITY", project_delta=5)
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, user, item, suffix="a"))
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, user, item, suffix="b"))

    assert app_module.purchased_project_capacity(user) == 10
    assert app_module.effective_project_limit(user) == 13


def test_capacity_purchase_replay_is_idempotent(app_module, db_session):
    user = _make_user(app_module, db_session, "replay-cap@example.com", limit=3)
    item = _catalog(app_module, db_session, "CAP_5R", "PROJECT_CAPACITY", project_delta=5)
    purchase = _purchase(app_module, db_session, user, item)

    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    replay = app_module.fulfill_addon_purchase(purchase)
    assert replay["success"] is True and replay["replay"] is True

    assert app_module.purchased_project_capacity(user) == 5
    assert app_module.effective_project_limit(user) == 8
    assert app_module.EntitlementTransaction.query.filter_by(
        source_type="addon_purchase", source_id=purchase.id
    ).count() == 1


def test_purchased_capacity_survives_subscription_expiry(app_module, db_session, plan):
    user = _make_user(app_module, db_session, "lapse@example.com", limit=3)
    item = _catalog(app_module, db_session, "CAP_LAPSE", "PROJECT_CAPACITY", project_delta=5)
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, user, item))

    user.subscription_status = "expired"
    user.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    # Ledger row is never deleted and stays auditable.
    assert app_module.purchased_project_capacity(user) == 5
    # And a later plan re-sync re-adds it instead of erasing it.
    assert app_module.reconciled_project_limit(user, plan.total_project_limit) == plan.total_project_limit + 5


def test_no_double_counting_between_limit_field_and_ledger(app_module, db_session):
    """subscribed_project_limit is the materialized effective value; a plan
    re-sync must land on plan+purchased exactly once, not plan+field+ledger."""
    user = _make_user(app_module, db_session, "nodouble@example.com", limit=3)
    item = _catalog(app_module, db_session, "CAP_ND", "PROJECT_CAPACITY", project_delta=5)
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, user, item))
    assert user.subscribed_project_limit == 8

    reconciled = app_module.reconciled_project_limit(user, 3)
    assert reconciled == 8
    user.subscribed_project_limit = reconciled
    db_session.commit()
    # Repeated reconciliation is stable (no drift/compounding).
    assert app_module.reconciled_project_limit(user, 3) == 8
    assert app_module.effective_project_limit(user) == 8


def test_project_creation_respects_effective_limit_and_delete_frees_slot(app_module, db_session, client):
    user = _make_user(app_module, db_session, "reserve@example.com", limit=1, used=0)
    assert app_module._reserve_project_quota_atomic(user) is True
    db_session.commit()
    db_session.refresh(user)
    assert user.projects_used == 1
    assert app_module._reserve_project_quota_atomic(user) is False

    item = _catalog(app_module, db_session, "CAP_RES", "PROJECT_CAPACITY", project_delta=1)
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, user, item))
    db_session.refresh(user)
    assert app_module.effective_project_limit(user) == 2
    assert app_module._reserve_project_quota_atomic(user) is True
    db_session.commit()
    db_session.refresh(user)
    assert user.projects_used == 2

    # Releasing a project returns the slot to the reusable pool.
    user.projects_used = max(0, user.projects_used - 1)
    db_session.commit()
    assert app_module._reserve_project_quota_atomic(user) is True


def test_transfer_consumes_and_frees_slot_only_on_completion(app_module, db_session):
    sender = _make_user(app_module, db_session, "t-sender@example.com", limit=3, used=1)
    recipient = _make_user(app_module, db_session, "t-recipient@example.com", limit=1, used=1)
    project = _make_project(app_module, db_session, sender)

    transfer = app_module.initiate_project_ownership_transfer(project, sender, recipient)
    db_session.commit()
    # Nothing moved on initiation.
    assert sender.projects_used == 1
    assert recipient.projects_used == 1
    assert transfer.status == "PENDING_ACCEPTANCE"

    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    assert transfer.status == "PENDING_CAPACITY"
    assert sender.projects_used == 1  # not freed while pending

    # Purchased capacity unblocks it.
    item = _catalog(app_module, db_session, "CAP_XFER", "PROJECT_CAPACITY", project_delta=1)
    app_module.fulfill_addon_purchase(_purchase(app_module, db_session, recipient, item))
    db_session.refresh(recipient)
    assert app_module.effective_project_limit(recipient) == 2

    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    db_session.refresh(sender)
    db_session.refresh(recipient)
    assert transfer.status == "COMPLETED"
    assert recipient.projects_used == 2  # consumed on completion
    assert sender.projects_used == 0  # freed on completion
    assert app_module.project_current_owner_user_id(project) == recipient.id


# ---------------------------------------------------------------------------
# B. Project-targeted entitlements
# ---------------------------------------------------------------------------
def test_account_addons_allow_null_project_and_reject_a_project_target(app_module, db_session):
    user = _make_user(app_module, db_session, "acct-target@example.com")
    item = _catalog(app_module, db_session, "SCANS_100", "EXTRA_SCANS", scan_delta=100)
    purchase = _purchase(app_module, db_session, user, item)
    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    tx = app_module.EntitlementTransaction.query.filter_by(source_id=purchase.id).one()
    assert tx.project_id is None

    project = _make_project(app_module, db_session, user)
    bad = _purchase(app_module, db_session, user, item, project=project, suffix="bad")
    result = app_module.fulfill_addon_purchase(bad)
    assert result["success"] is False
    assert result["code"] == "PROJECT_TARGET_INVALID"


def test_renewal_requires_project_id(app_module, db_session):
    user = _make_user(app_module, db_session, "renew-noproj@example.com")
    item = _catalog(app_module, db_session, "RENEW_365", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = _purchase(app_module, db_session, user, item)
    result = app_module.fulfill_addon_purchase(purchase)
    assert result["success"] is False
    assert result["code"] == "PROJECT_NOT_FOUND"

    with pytest.raises(ValueError):
        app_module._apply_entitlement_transaction(
            user, "PROJECT_SERVICE_COVERAGE", 365, "manual_test", 9999, "no project"
        )


def test_wrong_user_and_inaccessible_project_renewal_rejected(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-owner@example.com")
    stranger = _make_user(app_module, db_session, "renew-stranger@example.com")
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_365W", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)

    purchase = _purchase(app_module, db_session, stranger, item, project=project)
    result = app_module.fulfill_addon_purchase(purchase)
    assert result["success"] is False
    assert result["code"] == "PROJECT_FORBIDDEN"
    assert app_module.ProjectServiceCoverage.query.filter_by(project_id=project.id).count() == 0


def test_project_target_preserved_through_purchase_and_transaction(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-keep@example.com")
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_KEEP", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = _purchase(app_module, db_session, owner, item, project=project)

    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    tx = app_module.EntitlementTransaction.query.filter_by(source_id=purchase.id).one()
    assert tx.project_id == project.id
    assert purchase.project_id == project.id


# ---------------------------------------------------------------------------
# C. Renewal anchor / no-overlap
# ---------------------------------------------------------------------------
def test_expired_project_renewal_starts_now(app_module, db_session):
    owner = _make_user(app_module, db_session, "anchor-expired@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    now = datetime(2027, 3, 1)
    assert app_module.project_renewal_anchor(project, now) == now


def test_active_subscription_pushes_renewal_start_past_subscription_horizon(app_module, db_session):
    owner = _make_user(app_module, db_session, "anchor-sub@example.com")
    owner.subscription_expires_at = datetime(2026, 12, 31)
    db_session.commit()
    project = _make_project(app_module, db_session, owner)

    now = datetime(2026, 6, 1)
    anchor = app_module.project_renewal_anchor(project, now)
    assert anchor == datetime(2026, 12, 31)

    coverage = app_module.apply_standalone_project_renewal(project, owner, 365, source_id=1, now=now)
    db_session.commit()
    assert coverage.coverage_start == datetime(2026, 12, 31)
    assert coverage.coverage_end == datetime(2026, 12, 31) + timedelta(days=365)


def test_second_renewal_chains_without_overlap(app_module, db_session):
    owner = _make_user(app_module, db_session, "anchor-chain@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    now = datetime(2027, 1, 1)

    first = app_module.apply_standalone_project_renewal(project, owner, 365, source_id=1, now=now)
    db_session.commit()
    second = app_module.apply_standalone_project_renewal(project, owner, 365, source_id=2, now=now)
    db_session.commit()

    assert first.coverage_start == now
    assert second.coverage_start == first.coverage_end
    assert second.coverage_end == first.coverage_end + timedelta(days=365)
    # Contiguous, no overlap, nothing wasted.
    assert second.coverage_start >= first.coverage_end


def test_longest_horizon_wins_across_sources(app_module, db_session):
    owner = _make_user(app_module, db_session, "anchor-longest@example.com")
    owner.subscription_expires_at = datetime(2029, 1, 1)
    db_session.commit()
    project = _make_project(app_module, db_session, owner)
    app_module.add_project_service_coverage(
        project,
        "STANDALONE_PROJECT_RENEWAL",
        coverage_start=datetime(2027, 1, 1),
        coverage_end=datetime(2028, 1, 1),
    )
    db_session.commit()
    # Subscription reaches further than the project coverage -> subscription wins.
    assert app_module.project_renewal_anchor(project, datetime(2027, 6, 1)) == datetime(2029, 1, 1)


def test_renewal_does_not_modify_user_subscription_expires_at(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-nosub@example.com")
    original = owner.subscription_expires_at
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_NOSUB", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = _purchase(app_module, db_session, owner, item, project=project)

    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    db_session.refresh(owner)
    assert owner.subscription_expires_at == original


def test_renewal_creates_exactly_one_coverage_row_and_replay_creates_none(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-once@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_ONCE", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    purchase = _purchase(app_module, db_session, owner, item, project=project)

    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    replay = app_module.fulfill_addon_purchase(purchase)
    assert replay["replay"] is True

    rows = app_module.ProjectServiceCoverage.query.filter_by(
        project_id=project.id, source_type="STANDALONE_PROJECT_RENEWAL"
    ).all()
    assert len(rows) == 1
    assert app_module.EntitlementTransaction.query.filter_by(source_id=purchase.id).count() == 1


def test_expired_then_renewed_project_keeps_same_id_and_qr_and_goes_live(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-live@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    project_id, qr = project.id, project.qr_code_filename

    assert app_module.project_public_access_state(project)["is_live"] is False

    item = _catalog(app_module, db_session, "RENEW_LIVE", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    assert app_module.fulfill_addon_purchase(
        _purchase(app_module, db_session, owner, item, project=project)
    )["success"] is True
    db_session.refresh(project)

    state = app_module.project_public_access_state(project)
    assert state["is_live"] is True
    assert state["coverage_source"] == "STANDALONE_PROJECT_RENEWAL"
    assert project.id == project_id and project.qr_code_filename == qr


def test_suspended_project_stays_unavailable_despite_valid_coverage(app_module, db_session):
    owner = _make_user(app_module, db_session, "renew-suspended@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "RENEW_SUSP", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    assert app_module.fulfill_addon_purchase(
        _purchase(app_module, db_session, owner, item, project=project)
    )["success"] is True

    project.is_active = False
    db_session.commit()
    state = app_module.project_public_access_state(project)
    assert state["is_live"] is False
    assert state["reason"] == "inactive"
    # Suspension does not erase the paid horizon underneath.
    assert app_module.project_renewal_anchor(project) > datetime.utcnow()


def test_transfer_preserves_project_specific_coverage(app_module, db_session):
    sender = _make_user(app_module, db_session, "cov-sender@example.com", limit=3, used=1, active=False)
    recipient = _make_user(app_module, db_session, "cov-recipient@example.com", limit=3, used=0, expires_in_days=400)
    project = _make_project(app_module, db_session, sender)
    coverage = app_module.add_project_service_coverage(
        project,
        "STANDALONE_PROJECT_RENEWAL",
        coverage_start=datetime.utcnow() - timedelta(days=1),
        coverage_end=datetime.utcnow() + timedelta(days=100),
    )
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(project, sender, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()

    db_session.refresh(coverage)
    assert transfer.status == "COMPLETED"
    assert coverage.project_id == project.id
    assert coverage.status == "ACTIVE"
    assert coverage.coverage_end > datetime.utcnow()  # paid horizon untouched by transfer
    # Recipient's own subscription is simply another coverage source; the
    # longest horizon wins, so it can extend past the project's own coverage.
    assert app_module.project_renewal_anchor(project) == recipient.subscription_expires_at
    assert recipient.subscription_expires_at > coverage.coverage_end


def test_legacy_compatibility_indefinite_coverage_blocks_paid_renewal(app_module, db_session):
    owner = _make_user(app_module, db_session, "legacy@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    app_module.add_project_service_coverage(
        project, "LEGACY_COMPATIBILITY", coverage_end=None, reason="backfill"
    )
    db_session.commit()

    assert app_module.project_renewal_anchor(project) is None
    eligible, code, _msg = app_module.project_renewal_eligibility(project)
    assert eligible is False
    assert code == "COVERAGE_ALREADY_INDEFINITE"

    item = _catalog(app_module, db_session, "RENEW_LEG", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    result = app_module.fulfill_addon_purchase(_purchase(app_module, db_session, owner, item, project=project))
    assert result["success"] is False
    assert result["code"] == "COVERAGE_ALREADY_INDEFINITE"
    assert app_module.ProjectServiceCoverage.query.filter_by(
        project_id=project.id, source_type="STANDALONE_PROJECT_RENEWAL"
    ).count() == 0


# ---------------------------------------------------------------------------
# D. Admin grant
# ---------------------------------------------------------------------------
def test_admin_finite_grant_records_admin_and_reason_and_makes_project_covered(app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "grant-owner@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    assert app_module.project_public_access_state(project)["is_live"] is False

    coverage = app_module.admin_grant_project_service_coverage(project, admin, 30, "Goodwill credit")
    db_session.commit()

    assert coverage.source_type == "ADMIN_GRANT"
    assert coverage.coverage_end is not None  # finite only
    assert coverage.created_by_admin_id == admin.id
    assert coverage.reason == "Goodwill credit"
    assert app_module.project_public_access_state(project)["is_live"] is True
    assert app_module.AdminActivity.query.filter_by(activity_type="project_coverage_grant").count() == 1


def test_admin_grant_rejects_indefinite_and_missing_reason(app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "grant-bad@example.com", active=False)
    project = _make_project(app_module, db_session, owner)
    with pytest.raises(ValueError):
        app_module.admin_grant_project_service_coverage(project, admin, 0, "no duration")
    with pytest.raises(ValueError):
        app_module.admin_grant_project_service_coverage(project, admin, 30, "")


def test_suspended_project_stays_unavailable_despite_admin_grant(app_module, db_session, admin):
    owner = _make_user(app_module, db_session, "grant-susp@example.com", active=False)
    project = _make_project(app_module, db_session, owner, active=False)
    app_module.admin_grant_project_service_coverage(project, admin, 30, "Grant on suspended project")
    db_session.commit()
    assert app_module.project_public_access_state(project)["is_live"] is False


# ---------------------------------------------------------------------------
# E. Public content reporting
# ---------------------------------------------------------------------------
def test_anonymous_report_accepted_and_does_not_touch_project(app_module, db_session, client):
    owner = _make_user(app_module, db_session, "report-owner@example.com")
    project = _make_project(app_module, db_session, owner)

    response = client.post(
        f"/api/projects/{project.id}/report",
        json={"reason": "spam", "details": "Looks like spam."},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["success"] is True
    assert "report_id" not in body and "count" not in body

    report = app_module.ContentReport.query.filter_by(project_id=project.id).one()
    assert report.reporter_user_id is None
    assert report.status == "OPEN"
    assert report.reason == "SPAM"
    assert report.reporter_ip_hash and len(report.reporter_ip_hash) == 64
    db_session.refresh(project)
    assert project.is_active is True
    assert app_module.Project.query.get(project.id) is not None


def test_logged_in_reporter_identity_captured(app_module, db_session, client, normal_user):
    owner = _make_user(app_module, db_session, "report-owner2@example.com")
    project = _make_project(app_module, db_session, owner)
    _login(client, normal_user)

    assert client.post(f"/api/projects/{project.id}/report", json={"reason": "PRIVACY"}).status_code == 201
    report = app_module.ContentReport.query.filter_by(project_id=project.id).one()
    assert report.reporter_user_id == normal_user.id
    assert report.reporter_email == normal_user.email


def test_invalid_reason_and_details_length_rejected(app_module, db_session, client):
    owner = _make_user(app_module, db_session, "report-owner3@example.com")
    project = _make_project(app_module, db_session, owner)

    bad_reason = client.post(f"/api/projects/{project.id}/report", json={"reason": "I_DONT_LIKE_IT"})
    assert bad_reason.status_code == 400
    assert bad_reason.get_json()["code"] == "INVALID_REASON"

    too_long = client.post(
        f"/api/projects/{project.id}/report",
        json={"reason": "SPAM", "details": "x" * (app_module.CONTENT_REPORT_DETAILS_MAX + 1)},
    )
    assert too_long.status_code == 400
    assert too_long.get_json()["code"] == "DETAILS_TOO_LONG"
    assert app_module.ContentReport.query.count() == 0


def test_report_rate_limit_active(app_module, db_session, client):
    owner = _make_user(app_module, db_session, "report-rl@example.com")
    project = _make_project(app_module, db_session, owner)
    app_module.request_limiter.clear()
    limit, _window = app_module.RATE_LIMITS["content_report"]

    for _ in range(limit):
        assert client.post(f"/api/projects/{project.id}/report", json={"reason": "SPAM"}).status_code == 201
    blocked = client.post(f"/api/projects/{project.id}/report", json={"reason": "SPAM"})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "RATE_LIMITED"
    app_module.request_limiter.clear()


def test_report_on_missing_project_is_404(app_module, db_session, client):
    app_module.request_limiter.clear()
    assert client.post("/api/projects/99999/report", json={"reason": "SPAM"}).status_code == 404


# ---------------------------------------------------------------------------
# F. Admin moderation backend
# ---------------------------------------------------------------------------
def _make_report(app_module, db_session, project, reason="SPAM"):
    report = app_module.ContentReport(project_id=project.id, reason=reason, status="OPEN")
    db_session.add(report)
    db_session.commit()
    return report


def test_admin_report_list_authorized_and_unauthorized_blocked(app_module, db_session, client, admin):
    owner = _make_user(app_module, db_session, "mod-owner@example.com")
    project = _make_project(app_module, db_session, owner)
    _make_report(app_module, db_session, project)

    assert client.get("/admin/reports").status_code != 200  # not logged in as admin

    _login_admin_role(client, admin)
    response = client.get("/admin/reports")
    assert response.status_code == 200
    assert len(response.get_json()["reports"]) == 1


def test_admin_moderation_permission_codes_exist(app_module):
    for role in ("admin", "superadmin"):
        assert "admin.reports.view" in app_module.ADMIN_ROLE_PERMISSIONS[role]
        assert "admin.reports.manage" in app_module.ADMIN_ROLE_PERMISSIONS[role]


def test_admin_report_transitions_under_review_and_dismissed(app_module, db_session, client, admin):
    owner = _make_user(app_module, db_session, "mod-owner2@example.com")
    project = _make_project(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin_role(client, admin)

    assert client.post(f"/admin/reports/{report.id}/review", json={"status": "UNDER_REVIEW"}).status_code == 200
    db_session.refresh(report)
    assert report.status == "UNDER_REVIEW"
    assert report.reviewed_by_admin_id == admin.id

    assert client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "DISMISSED", "resolution_reason": "Not a violation."},
    ).status_code == 200
    db_session.refresh(report)
    assert report.status == "DISMISSED"
    assert report.reviewed_at is not None
    assert report.resolution_reason == "Not a violation."
    db_session.refresh(project)
    assert project.is_active is True


def test_action_taken_project_suspended_sets_is_active_false_without_deleting(app_module, db_session, client, admin):
    owner = _make_user(app_module, db_session, "mod-owner3@example.com")
    project = _make_project(app_module, db_session, owner)
    pair = app_module.ProjectPair(project_id=project.id, pair_index=1, video_filename="v.mp4")
    db_session.add(pair)
    db_session.commit()
    report = _make_report(app_module, db_session, project, reason="EXPLICIT_OR_INAPPROPRIATE")
    _login_admin_role(client, admin)

    response = client.post(
        f"/admin/reports/{report.id}/review",
        json={
            "status": "ACTION_TAKEN",
            "resolution_action": "PROJECT_SUSPENDED",
            "resolution_reason": "Confirmed policy violation.",
        },
    )
    assert response.status_code == 200
    db_session.refresh(report)
    db_session.refresh(project)
    assert report.status == "ACTION_TAKEN"
    assert report.resolution_action == "PROJECT_SUSPENDED"
    assert report.reviewed_by_admin_id == admin.id and report.reviewed_at is not None
    assert project.is_active is False
    # Nothing hard-deleted.
    assert app_module.Project.query.get(project.id) is not None
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1
    assert app_module.User.query.get(owner.id).is_blocked in (False, None)
    assert app_module.AdminActivity.query.filter_by(activity_type="content_report_review").count() == 1


def test_admin_review_rejects_invalid_status_and_action(app_module, db_session, client, admin):
    owner = _make_user(app_module, db_session, "mod-owner4@example.com")
    project = _make_project(app_module, db_session, owner)
    report = _make_report(app_module, db_session, project)
    _login_admin_role(client, admin)

    assert client.post(f"/admin/reports/{report.id}/review", json={"status": "OPEN"}).status_code == 400
    assert client.post(f"/admin/reports/{report.id}/review", json={"status": "BANNED"}).status_code == 400
    assert client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "ACTION_TAKEN", "resolution_action": "DELETE_EVERYTHING"},
    ).status_code == 400


# ---------------------------------------------------------------------------
# G. API surfaces
# ---------------------------------------------------------------------------
def test_capacity_and_coverage_api_surfaces(app_module, db_session, client):
    user = _make_user(app_module, db_session, "api-surface@example.com", limit=3)
    project = _make_project(app_module, db_session, user)
    _login(client, user)

    capacity = client.get("/api/account/capacity")
    assert capacity.status_code == 200
    assert capacity.get_json()["capacity"]["effective_project_limit"] == 3

    coverage = client.get(f"/api/projects/{project.id}/coverage")
    assert coverage.status_code == 200
    body = coverage.get_json()["coverage"]
    assert body["is_live"] is True
    assert body["renewal_eligible"] is True
    assert body["renewal_starts_at"] is not None

    stranger = _make_user(app_module, db_session, "api-stranger@example.com")
    _login(client, stranger)
    assert client.get(f"/api/projects/{project.id}/coverage").status_code == 404


def test_addon_catalog_now_exposes_capacity_and_renewal_types(app_module, db_session, client):
    user = _make_user(app_module, db_session, "api-catalog@example.com")
    _catalog(app_module, db_session, "CAT_CAP", "PROJECT_CAPACITY", project_delta=5)
    _catalog(app_module, db_session, "CAT_RENEW", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)
    _login(client, user)

    response = client.get("/api/addons/catalog")
    assert response.status_code == 200
    types = {item["addon_type"] for item in response.get_json()["addons"]}
    assert {"PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE"} <= types
    assert "PROJECT_CAPACITY" in app_module.ADDON_PURCHASABLE_TYPES


def test_renewal_order_requires_authorized_project(app_module, db_session, client):
    owner = _make_user(app_module, db_session, "order-owner@example.com")
    stranger = _make_user(app_module, db_session, "order-stranger@example.com")
    project = _make_project(app_module, db_session, owner)
    item = _catalog(app_module, db_session, "ORDER_RENEW", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365)

    _login(client, stranger)
    assert client.post("/api/addons/orders", json={"catalog_id": item.id, "project_id": project.id}).status_code == 404
    _login(client, owner)
    assert client.post("/api/addons/orders", json={"catalog_id": item.id}).status_code == 404

    cap = _catalog(app_module, db_session, "ORDER_CAP", "PROJECT_CAPACITY", project_delta=5)
    account_level_with_project = client.post(
        "/api/addons/orders", json={"catalog_id": cap.id, "project_id": project.id}
    )
    assert account_level_with_project.status_code == 400
    assert account_level_with_project.get_json()["code"] == "PROJECT_TARGET_INVALID"
