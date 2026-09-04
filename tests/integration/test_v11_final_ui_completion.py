"""V1.1 final UI completion lane (frontend HIGH findings PAY-2, OWN-2, COV-1/2/3).

Every assertion here is about PRESENTATION of an already-shipped backend contract:
the refund attention worklist, per-project coverage badges/warnings, the admin
coverage-grant control, the claimant discovery entry point, transfer expiry, and
the vendor-before-admin claim gate. Nothing in this lane implements a business
rule, so nothing here asserts one - the tests check that the screens tell the
truth about what the backend already decided, and that they never offer an action
the backend would refuse.

Focused scope by policy: the full suite and the PostgreSQL certification lane are
the project lead's, run once after this lane merges.
"""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _user(app_module, db_session, email, *, status="active", expires_in_days=30,
          account_type="INDIVIDUAL", limit=5):
    expires = None
    if expires_in_days is not None:
        expires = datetime.utcnow() + timedelta(days=expires_in_days)
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        account_type=account_type,
        subscription_status=status,
        subscription_expires_at=expires,
        subscribed_project_limit=limit,
        subscribed_scan_limit=100,
        projects_used=0,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _project(app_module, db_session, owner, *, name="UI Project", vendor=None, active=True, index=1):
    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        manager_vendor_user_id=vendor.id if vendor else None,
        user_project_index=index,
        scanner_url=f"/scanner/ui{index}",
        qr_code_filename=f"project_ui{index}.png",
        qr_code_path=f"/qr/project_ui{index}.png",
        is_active=active,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _catalog(app_module, db_session, code="UIEXTRA10"):
    item = app_module.AddonCatalog(
        code=code,
        name=code,
        addon_type="EXTRA_SCANS",
        unit_amount=99.0,
        currency="INR",
        scan_delta=10,
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _purchase(app_module, db_session, user, item, suffix="1"):
    purchase = app_module.AddonPurchase(
        order_id=f"ADDON_UI_{item.code}_{suffix}",
        user_id=user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id=f"order_ui_{suffix}",
        razorpay_payment_id=f"pay_ui_{suffix}",
    )
    db_session.add(purchase)
    db_session.commit()
    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    db_session.refresh(purchase)
    return purchase


def _refund(app_module, db_session, admin, user, purchase, *, status, reconciliation,
            provider_refund_id=None, reconciliation_message=None, failure_message=None,
            failure_code=None):
    refund = app_module.PaymentRefund(
        addon_purchase_id=purchase.id,
        user_id=user.id,
        provider="RAZORPAY",
        provider_payment_id=purchase.razorpay_payment_id,
        provider_refund_id=provider_refund_id,
        amount=purchase.total_amount,
        currency=purchase.currency,
        status=status,
        reconciliation_status=reconciliation,
        reconciliation_message_safe=reconciliation_message,
        failure_code=failure_code,
        failure_message_safe=failure_message,
        reason="ui lane test",
        requested_by_admin_id=admin.id,
        requested_at=datetime.utcnow(),
        idempotency_key=f"refund:addon_purchase:{purchase.id}",
    )
    db_session.add(refund)
    db_session.commit()
    return refund


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


@pytest.fixture()
def plain_admin(app_module, db_session, admin):
    """Role "admin": has admin.payments.view and admin.projects.view, but NOT
    admin.payments.refund and NOT superadmin.capacity.manage."""
    other = app_module.Admin(
        email="plain-admin@example.com",
        name="Plain Admin",
        password_hash=generate_password_hash("AdminPass123"),
        role="admin",
        is_active=True,
        created_by=admin.id,
    )
    db_session.add(other)
    db_session.commit()
    return other


# ===========================================================================
# F1  Refund attention / recovery worklist  (PAY-2)
# ===========================================================================
def test_attention_worklist_lists_every_unsettled_axis_and_excludes_settled(
    app_module, db_session, client, admin
):
    user = _user(app_module, db_session, "refund-ui@example.com")
    item = _catalog(app_module, db_session)
    rows = {}
    for suffix, status, reconciliation in [
        ("f", "REFUND_FAILED", "PENDING"),
        ("p", "REFUND_PROCESSING", "PENDING"),
        ("r", "REFUNDED", "FAILED"),
        ("m", "REFUNDED", "MANUAL_REVIEW_REQUIRED"),
        ("s", "REFUNDED", "APPLIED"),
    ]:
        purchase = _purchase(app_module, db_session, user, item, suffix=suffix)
        rows[suffix] = _refund(
            app_module, db_session, admin, user, purchase,
            status=status, reconciliation=reconciliation,
            provider_refund_id=f"rfnd_ui_{suffix}",
        )

    # The screen's own source of truth is the shared backend predicate.
    attention_ids = {r.id for r in app_module.stuck_refund_query().all()}
    assert rows["s"].id not in attention_ids

    _login_admin(client, admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    assert 'data-testid="refund-attention-worklist"' in html
    for suffix in ("f", "p", "r", "m"):
        assert f'data-refund-id="{rows[suffix].id}"' in html
    # Settled (REFUNDED + APPLIED) never appears in the attention view.
    assert f'data-refund-id="{rows["s"].id}"' not in html


def test_worklist_renders_both_status_axes_separately(app_module, db_session, client, admin):
    user = _user(app_module, db_session, "refund-axes@example.com")
    item = _catalog(app_module, db_session)
    purchase = _purchase(app_module, db_session, user, item)
    _refund(
        app_module, db_session, admin, user, purchase,
        status="REFUNDED", reconciliation="FAILED",
        provider_refund_id="rfnd_axes",
        reconciliation_message="Entitlement reversal could not be completed.",
    )

    _login_admin(client, admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    # Provider outcome and local reconciliation are never merged into one verdict.
    assert 'data-refund-status="REFUNDED"' in html
    assert 'data-reconciliation-status="FAILED"' in html
    assert app_module.REFUND_STATUS_LABELS["REFUNDED"] in html
    assert app_module.REFUND_RECONCILIATION_LABELS["FAILED"] in html
    assert "Entitlement reversal could not be completed." in html


def test_manual_review_row_offers_no_retry_and_says_a_human_must_decide(
    app_module, db_session, client, admin
):
    user = _user(app_module, db_session, "refund-manual@example.com")
    item = _catalog(app_module, db_session)
    manual = _refund(
        app_module, db_session, admin, user,
        _purchase(app_module, db_session, user, item, suffix="m"),
        status="REFUNDED", reconciliation="MANUAL_REVIEW_REQUIRED", provider_refund_id="rfnd_m",
    )
    retryable = _refund(
        app_module, db_session, admin, user,
        _purchase(app_module, db_session, user, item, suffix="f"),
        status="REFUND_FAILED", reconciliation="PENDING",
    )

    _login_admin(client, admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    manual_row = html.split(f'data-refund-id="{manual.id}"')[1].split("</tr>")[0]
    retry_row = html.split(f'data-refund-id="{retryable.id}"')[1].split("</tr>")[0]

    # Manual review is genuinely not auto-fixable: no action, and it says so.
    assert 'data-testid="refund-no-retry"' in manual_row
    assert "refund-recover-btn" not in manual_row
    assert "not automatically fixable" in manual_row
    assert "Retrying will not resolve it" in manual_row
    # A retryable row does get the safe recovery action.
    assert "refund-recover-btn" in retry_row
    assert f"/admin/api/refunds/{retryable.id}/recover" in retry_row


def test_recovery_action_hidden_without_the_refund_permission(
    app_module, db_session, client, admin, plain_admin, monkeypatch
):
    user = _user(app_module, db_session, "refund-perm@example.com")
    item = _catalog(app_module, db_session)
    _refund(
        app_module, db_session, admin, user,
        _purchase(app_module, db_session, user, item),
        status="REFUND_FAILED", reconciliation="PENDING",
    )

    # The operations page itself needs superadmin.operations.view, so grant the
    # page but keep the real refund-permission answer for the action.
    monkeypatch.setattr(
        app_module, "admin_has_permission",
        lambda a, permission: permission in {"superadmin.operations.view", "admin.payments.view"},
    )
    _login_admin(client, plain_admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    assert 'data-testid="refund-attention-worklist"' in html
    assert "refund-recover-btn" not in html
    assert "Recovery needs the refund permission" in html


def _webhook_event(app_module, db_session, *, purchase=None, key="ui-oob-1",
                   payload_hash="HASH-SENTINEL-DO-NOT-RENDER"):
    """The raw provider body is deliberately never persisted by this codebase -
    only a hash fingerprint - so the only provider-shaped strings that could leak
    are the fingerprint and the idempotency key."""
    event = app_module.RazorpayWebhookEvent(
        idempotency_key=key,
        event_type="refund.processed",
        payload_hash=payload_hash,
        processing_status="failed",
        failure_code=app_module.OUT_OF_BAND_REFUND_FAILURE_CODE,
        addon_purchase_id=purchase.id if purchase else None,
    )
    db_session.add(event)
    db_session.commit()
    return event


def test_worklist_leaks_no_provider_fingerprint_or_payment_id(app_module, db_session, client, admin):
    user = _user(app_module, db_session, "refund-leak@example.com")
    item = _catalog(app_module, db_session)
    purchase = _purchase(app_module, db_session, user, item)
    _refund(
        app_module, db_session, admin, user, purchase,
        status="REFUND_FAILED", reconciliation="PENDING",
        failure_code="PROVIDER_REQUEST_FAILED",
        failure_message="The payment provider rejected the refund request.",
    )
    _webhook_event(app_module, db_session, purchase=purchase, key="ui-oob-leak")

    _login_admin(client, admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    # The safe operator message is shown...
    assert "The payment provider rejected the refund request." in html
    # ...and nothing provider-shaped is.
    assert "HASH-SENTINEL-DO-NOT-RENDER" not in html
    assert "payload_hash" not in html
    assert "ui-oob-leak" not in html
    assert purchase.razorpay_payment_id not in html


def test_out_of_band_block_shows_correlated_purchase_and_manual_review_state(
    app_module, db_session, client, admin
):
    user = _user(app_module, db_session, "refund-oob@example.com")
    item = _catalog(app_module, db_session)
    purchase = _purchase(app_module, db_session, user, item)
    _webhook_event(app_module, db_session, purchase=purchase, key="ui-oob-visible")

    _login_admin(client, admin)
    html = client.get("/admin/payments/refunds").get_data(as_text=True)

    assert 'data-testid="refund-out-of-band-row"' in html
    assert f"Add-on purchase #{purchase.id}" in html
    assert "Manual review required" in html
    # No local refund record was fabricated for it.
    assert app_module.PaymentRefund.query.count() == 0


def test_recovery_route_is_post_only_and_still_permission_gated(
    app_module, db_session, client, admin, plain_admin
):
    user = _user(app_module, db_session, "refund-gate@example.com")
    item = _catalog(app_module, db_session)
    refund = _refund(
        app_module, db_session, admin, user,
        _purchase(app_module, db_session, user, item),
        status="REFUND_FAILED", reconciliation="PENDING",
    )

    _login_admin(client, admin)
    assert client.get(f"/admin/api/refunds/{refund.id}/recover").status_code == 405

    # Role "admin" has payments.view but not payments.refund - unchanged by this lane.
    assert app_module.admin_has_permission(plain_admin, "admin.payments.view") is True
    assert app_module.admin_has_permission(plain_admin, "admin.payments.refund") is False
    _login_admin(client, plain_admin)
    denied = client.post(f"/admin/api/refunds/{refund.id}/recover", json={"apply": True})
    assert denied.status_code in (302, 403)


def test_recovery_mutation_requires_csrf_when_enabled(app_module, db_session, client, admin):
    user = _user(app_module, db_session, "refund-csrf@example.com")
    item = _catalog(app_module, db_session)
    refund = _refund(
        app_module, db_session, admin, user,
        _purchase(app_module, db_session, user, item),
        status="REFUND_FAILED", reconciliation="PENDING",
    )
    _login_admin(client, admin)
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(f"/admin/api/refunds/{refund.id}/recover", json={"apply": True})
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = False
    assert response.status_code == 400


# ===========================================================================
# F2  Project coverage badges / expiry warnings  (COV-1, COV-2)
# ===========================================================================
def test_active_coverage_badge_shows_the_backend_supplied_end_date(app_module, db_session, client):
    owner = _user(app_module, db_session, "cov-active@example.com")
    project = _project(app_module, db_session, owner, name="Active Story")
    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "active"

    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="active"' in html
    assert app_module.PROJECT_COVERAGE_STATE_LABELS["active"] in html
    assert "until " in html
    assert 'data-coverage-warning' not in html


def test_indefinite_coverage_renders_no_end_date_instead_of_a_fake_one(app_module, db_session, client):
    owner = _user(app_module, db_session, "cov-indef@example.com", expires_in_days=None)
    project = _project(app_module, db_session, owner, name="Indefinite Story")
    summary = app_module.project_coverage_summary(project)
    assert summary["coverage_state"] == "active"
    assert summary["effective_coverage_until"] is None

    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="active"' in html
    assert "no end date" in html
    assert "until " not in html.split('data-coverage-state="active"')[1].split("</span>")[0]


def test_expired_coverage_badge_and_warning(app_module, db_session, client):
    owner = _user(app_module, db_session, "cov-expired@example.com", status="expired", expires_in_days=-5)
    project = _project(app_module, db_session, owner, name="Expired Story")
    assert app_module.project_coverage_summary(project)["coverage_state"] == "expired"

    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="expired"' in html
    assert app_module.PROJECT_COVERAGE_STATE_LABELS["expired"] in html
    assert 'data-coverage-warning="expired"' in html
    assert "renew coverage" in html
    # Never implies the media or QR are gone.
    assert "media and QR code are kept" in html


def test_no_coverage_badge_and_warning(app_module, db_session, client):
    owner = _user(app_module, db_session, "cov-none@example.com", status="expired", expires_in_days=None)
    project = _project(app_module, db_session, owner, name="Uncovered Story")
    assert app_module.project_coverage_summary(project)["coverage_state"] == "none"

    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="none"' in html
    assert app_module.PROJECT_COVERAGE_STATE_LABELS["none"] in html
    assert 'data-coverage-warning="none"' in html


@pytest.mark.parametrize("status,expires_in_days", [("active", 30), ("expired", -5)])
def test_suspended_never_renders_as_expired_or_active(app_module, db_session, client, status, expires_in_days):
    owner = _user(app_module, db_session, f"cov-susp-{status}@example.com",
                  status=status, expires_in_days=expires_in_days)
    project = _project(app_module, db_session, owner, name="Suspended Story", active=False)
    assert app_module.project_coverage_summary(project)["coverage_state"] == "suspended"

    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="suspended"' in html
    assert app_module.PROJECT_COVERAGE_STATE_LABELS["suspended"] in html
    assert 'data-coverage-warning="suspended"' in html
    # Distinct from BOTH plain states, in text as well as colour.
    assert 'data-coverage-state="expired"' not in html
    assert 'data-coverage-state="active"' not in html
    assert "buying coverage will not lift a suspension" in html


def test_card_branches_only_on_the_backend_coverage_state_string(
    app_module, db_session, client, monkeypatch
):
    """Proof the template does no date math: an expired project whose resolver
    says "active" must render as active."""
    owner = _user(app_module, db_session, "cov-authority@example.com", status="expired", expires_in_days=-5)
    project = _project(app_module, db_session, owner, name="Resolver Authority")
    assert app_module.project_coverage_summary(project)["coverage_state"] == "expired"

    monkeypatch.setattr(app_module, "project_coverage_state", lambda p, s, n: "active")
    _login(client, owner)
    html = client.get("/projects").get_data(as_text=True)

    assert 'data-coverage-state="active"' in html
    assert 'data-coverage-state="expired"' not in html


# ===========================================================================
# F3  Admin coverage grant control  (COV-3)
# ===========================================================================
def test_coverage_grant_form_present_for_authorized_admin_with_real_field_names(
    app_module, db_session, client, admin
):
    owner = _user(app_module, db_session, "grant-owner@example.com")
    project = _project(app_module, db_session, owner)

    _login_admin(client, admin)
    html = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)

    assert f"/admin/projects/{project.id}/service-coverage/grant" in html
    assert 'name="days"' in html
    assert 'name="reason"' in html
    assert 'name="csrf_token"' in html
    assert 'required' in html.split('id="coverageGrantReason"')[0].split('id="coverageGrantDays"')[1]


def test_coverage_grant_copy_separates_coverage_from_subscription_and_offers_no_revoke(
    app_module, db_session, client, admin
):
    owner = _user(app_module, db_session, "grant-copy@example.com")
    project = _project(app_module, db_session, owner)

    _login_admin(client, admin)
    html = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)

    assert 'data-testid="coverage-grant-explainer"' in html
    assert "separate from the account's subscription" in html
    assert "does not change ownership" in html
    # No revoke endpoint exists, so no revoke control was invented.
    assert "service-coverage/revoke" not in html
    assert "Revoke Coverage" not in html


def test_coverage_grant_form_hidden_from_admin_without_capacity_permission(
    app_module, db_session, client, plain_admin
):
    owner = _user(app_module, db_session, "grant-denied@example.com")
    project = _project(app_module, db_session, owner)
    assert app_module.admin_has_permission(plain_admin, "superadmin.capacity.manage") is False

    _login_admin(client, plain_admin)
    response = client.get(f"/admin/projects/{project.id}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    # No grant control and no grant endpoint reference: the only remaining mention
    # of the form is the inert lookup in the page's own script, which finds nothing.
    assert 'data-testid="coverage-grant-explainer"' not in html
    assert 'name="days"' not in html
    assert f"/admin/projects/{project.id}/service-coverage/grant" not in html


def test_admin_project_page_shows_the_resolver_coverage_state_not_a_recomputation(
    app_module, db_session, client, admin
):
    owner = _user(app_module, db_session, "grant-state@example.com", status="expired", expires_in_days=-5)
    project = _project(app_module, db_session, owner, active=False)
    assert app_module.project_coverage_summary(project)["coverage_state"] == "suspended"

    _login_admin(client, admin)
    html = client.get(f"/admin/projects/{project.id}").get_data(as_text=True)

    assert 'data-coverage-state="suspended"' in html
    assert app_module.PROJECT_COVERAGE_STATE_LABELS["suspended"] in html


# ===========================================================================
# F4  Claimant discovery / claim entry point  (OWN-2)
# ===========================================================================
def test_ownership_center_offers_the_claim_lookup_with_review_only_language(
    app_module, db_session, client
):
    claimant = _user(app_module, db_session, "claimant-ui@example.com")
    _login(client, claimant)
    html = client.get("/ownership").get_data(as_text=True)

    assert 'data-testid="claim-lookup"' in html
    assert "claimLookupForm" in html
    assert "/api/ownership/claim-lookup/" in html
    assert "never transfers a ScanStory" in html
    # No raw enum ever reaches the page.
    for code in ("CLAIMABLE", "ALREADY_OPEN", "NOT_CLAIMABLE"):
        assert code not in html


def test_lookup_reports_claimable_for_a_legitimate_non_owner(app_module, db_session, client):
    owner = _user(app_module, db_session, "lookup-owner@example.com")
    claimant = _user(app_module, db_session, "lookup-claimant@example.com")
    project = _project(app_module, db_session, owner, name="Claimable Story")

    _login(client, claimant)
    payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()

    assert payload["eligible"] is True
    assert payload["claim_url"] == f"/projects/{project.id}/ownership-claim"
    assert payload["project"]["name"] == "Claimable Story"
    assert "Nothing changes until it is reviewed" in payload["reason"]


def test_owner_and_manager_get_no_self_claim_offer(app_module, db_session, client):
    owner = _user(app_module, db_session, "self-owner@example.com")
    vendor = _user(app_module, db_session, "self-vendor@example.com", account_type="BUSINESS_VENDOR")
    project = _project(app_module, db_session, owner, vendor=vendor)

    for actor in (owner, vendor):
        _login(client, actor)
        payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()
        assert payload["eligible"] is False
        assert payload["claim_url"] is None
        assert payload["project"] is None


def test_non_eligible_answers_are_indistinguishable_from_a_missing_project(
    app_module, db_session, client
):
    owner = _user(app_module, db_session, "opaque-owner@example.com")
    suspended = _project(app_module, db_session, owner, name="Hidden Story", active=False)
    claimant = _user(app_module, db_session, "opaque-claimant@example.com")

    _login(client, claimant)
    real = client.get(f"/api/ownership/claim-lookup/{suspended.id}").get_json()
    missing = client.get("/api/ownership/claim-lookup/987654").get_json()

    assert real == missing
    assert "Hidden Story" not in str(real)


def test_duplicate_active_claim_returns_prose_not_a_raw_code(app_module, db_session, client):
    owner = _user(app_module, db_session, "dupe-owner@example.com")
    claimant = _user(app_module, db_session, "dupe-claimant@example.com")
    project = _project(app_module, db_session, owner)
    app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()

    _login(client, claimant)
    payload = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()

    assert payload["eligible"] is False
    assert payload["existing_claim_id"] is not None
    assert "already have an open ownership review request" in payload["reason"]
    assert payload["reason"] != payload["reason_code"]


def test_claim_submission_still_leaves_ownership_unchanged(app_module, db_session, client):
    owner = _user(app_module, db_session, "claimsub-owner@example.com")
    claimant = _user(app_module, db_session, "claimsub-claimant@example.com")
    project = _project(app_module, db_session, owner)

    _login(client, claimant)
    lookup = client.get(f"/api/ownership/claim-lookup/{project.id}").get_json()
    client.post(lookup["claim_url"], data={"evidence_summary": "printed for me"})
    db_session.expire_all()

    assert app_module.Project.query.get(project.id).current_owner_user_id == owner.id


# ===========================================================================
# F5  Transfer expiry / EXPIRED presentation  (OWN-3 presentation)
# ===========================================================================
def _transfer(app_module, db_session, project, sender, recipient, *, status, expires_at=None):
    transfer = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=sender.id,
        from_owner_user_id=sender.id,
        to_user_id=recipient.id,
        status=status,
        expires_at=expires_at,
    )
    db_session.add(transfer)
    db_session.commit()
    return transfer


def test_pending_transfer_shows_its_deadline_to_the_recipient(app_module, db_session, client):
    sender = _user(app_module, db_session, "exp-sender@example.com")
    recipient = _user(app_module, db_session, "exp-recipient@example.com")
    project = _project(app_module, db_session, sender)
    deadline = datetime.utcnow() + timedelta(days=14)
    transfer = _transfer(
        app_module, db_session, project, sender, recipient,
        status="PENDING_ACCEPTANCE", expires_at=deadline,
    )

    _login(client, recipient)
    html = client.get("/ownership").get_data(as_text=True)

    assert f'data-transfer-deadline="{transfer.id}"' in html
    assert deadline.strftime("%d %b %Y") in html
    assert "Accept handover" in html


def test_expired_transfer_is_terminal_with_no_action_controls(app_module, db_session, client):
    sender = _user(app_module, db_session, "exp2-sender@example.com")
    recipient = _user(app_module, db_session, "exp2-recipient@example.com")
    project = _project(app_module, db_session, sender)
    _transfer(
        app_module, db_session, project, sender, recipient,
        status="EXPIRED", expires_at=datetime.utcnow() - timedelta(days=1),
    )

    _login(client, recipient)
    html = client.get("/ownership").get_data(as_text=True)

    assert 'data-testid="expired-transfers"' in html
    assert 'data-transfer-status="EXPIRED"' in html
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["EXPIRED"] in html
    assert "Ownership did not move" in html
    # No accept / decline / retry control anywhere for an expired handover.
    assert "Accept handover" not in html
    assert "Retry capacity check" not in html
    assert "/accept" not in html


def test_pending_capacity_and_disputed_copy_is_unchanged_by_the_expiry_work(
    app_module, db_session, client
):
    sender = _user(app_module, db_session, "exp3-sender@example.com")
    recipient = _user(app_module, db_session, "exp3-recipient@example.com")
    capacity_project = _project(app_module, db_session, sender, name="Capacity", index=1)
    disputed_project = _project(app_module, db_session, sender, name="Disputed", index=2)
    _transfer(app_module, db_session, capacity_project, sender, recipient, status="PENDING_CAPACITY")
    _transfer(app_module, db_session, disputed_project, sender, recipient, status="DISPUTED")

    _login(client, recipient)
    html = client.get("/ownership").get_data(as_text=True)

    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["PENDING_CAPACITY"] in html
    assert "Retry uses this same handover" in html
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["DISPUTED"] in html
    assert "under manual ScanStory review" in html
    # Neither is presented as expired.
    assert 'data-transfer-status="EXPIRED"' not in html


def test_admin_ownership_page_distinguishes_expired_from_pending_and_disputed(
    app_module, db_session, client, admin
):
    sender = _user(app_module, db_session, "exp4-sender@example.com")
    recipient = _user(app_module, db_session, "exp4-recipient@example.com")
    project = _project(app_module, db_session, sender)
    _transfer(
        app_module, db_session, project, sender, recipient,
        status="EXPIRED", expires_at=datetime.utcnow() - timedelta(days=2),
    )
    pending = _transfer(
        app_module, db_session, project, sender, recipient,
        status="PENDING_ACCEPTANCE", expires_at=datetime.utcnow() + timedelta(days=10),
    )

    _login_admin(client, admin)
    html = client.get("/admin/ownership").get_data(as_text=True)

    assert 'data-transfer-status="EXPIRED"' in html
    assert 'data-testid="transfer-expired-note"' in html
    assert "does not cancel a linked claim" in html
    assert 'data-testid="transfer-deadline"' in html
    assert 'data-transfer-status="PENDING_ACCEPTANCE"' in html
    expired_cell = html.split('data-transfer-status="EXPIRED"')[1].split("</td>")[0]
    assert "no action is available" in expired_cell
    assert str(pending.id) in html


def test_expiring_a_transfer_does_not_hide_or_cancel_its_linked_claim(app_module, db_session, client):
    owner = _user(app_module, db_session, "exp5-owner@example.com")
    claimant = _user(app_module, db_session, "exp5-claimant@example.com")
    project = _project(app_module, db_session, owner)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()
    transfer = _transfer(
        app_module, db_session, project, owner, claimant,
        status="PENDING_ACCEPTANCE", expires_at=datetime.utcnow() - timedelta(days=1),
    )
    claim.transfer_id = transfer.id
    db_session.commit()

    assert app_module.expire_transfer_if_due(transfer) is True
    db_session.commit()
    db_session.expire_all()
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == "OPEN"

    _login(client, claimant)
    html = client.get("/ownership").get_data(as_text=True)

    # The expired handover is terminal, and the claim is still described separately.
    assert 'data-transfer-status="EXPIRED"' in html
    assert 'data-claim-status="OPEN"' in html
    assert 'data-claim-explainer="awaiting_response"' in html


# ===========================================================================
# F6  Vendor-awaiting-response claim presentation  (OWN-4 presentation)
# ===========================================================================
def test_vendor_managed_open_claim_shows_no_premature_admin_adjudication(
    app_module, db_session, client, admin
):
    owner = _user(app_module, db_session, "vend-owner@example.com")
    vendor = _user(app_module, db_session, "vend-vendor@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, "vend-claimant@example.com")
    project = _project(app_module, db_session, owner, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()

    # The template's visibility mirrors this exact backend answer.
    assert app_module.claim_admin_review_block_reason(claim) is not None

    _login_admin(client, admin)
    html = client.get("/admin/ownership").get_data(as_text=True)

    assert 'data-testid="claim-vendor-block"' in html
    assert "Waiting on the managing vendor" in html
    assert f"/admin/ownership/claims/{claim.id}/approve" not in html
    assert f"/admin/ownership/claims/{claim.id}/reject" not in html


def test_admin_controls_appear_once_the_vendor_has_responded(app_module, db_session, client, admin):
    owner = _user(app_module, db_session, "vend2-owner@example.com")
    vendor = _user(app_module, db_session, "vend2-vendor@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, "vend2-claimant@example.com")
    project = _project(app_module, db_session, owner, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()
    app_module.respond_to_project_ownership_claim(claim, vendor, False, response_note="not mine to give")
    db_session.commit()

    assert claim.status == "PENDING_ADMIN_REVIEW"
    assert app_module.claim_admin_review_block_reason(claim) is None

    _login_admin(client, admin)
    html = client.get("/admin/ownership").get_data(as_text=True)

    assert f"/admin/ownership/claims/{claim.id}/approve" in html
    assert f"/admin/ownership/claims/{claim.id}/reject" in html
    assert 'data-testid="claim-vendor-block"' not in html


def test_no_vendor_direct_admin_path_is_not_blocked(app_module, db_session, client, admin):
    owner = _user(app_module, db_session, "novend-owner@example.com")
    claimant = _user(app_module, db_session, "novend-claimant@example.com")
    project = _project(app_module, db_session, owner)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()

    assert claim.status == "OPEN"
    assert project.manager_vendor_user_id is None
    assert app_module.claim_admin_review_block_reason(claim) is None

    _login_admin(client, admin)
    html = client.get("/admin/ownership").get_data(as_text=True)

    assert f"/admin/ownership/claims/{claim.id}/approve" in html
    assert 'data-testid="claim-vendor-block"' not in html


def test_terminal_claims_get_no_decision_control_and_no_vendor_block_message(
    app_module, db_session, client, admin
):
    owner = _user(app_module, db_session, "term-owner@example.com")
    vendor = _user(app_module, db_session, "term-vendor@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, "term-claimant@example.com")
    project = _project(app_module, db_session, owner, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()
    app_module.cancel_project_ownership_claim(claim, claimant, reason="changed my mind")
    db_session.commit()

    _login_admin(client, admin)
    html = client.get("/admin/ownership").get_data(as_text=True)

    assert 'data-claim-status="CANCELLED"' in html
    assert "No available decision" in html
    assert 'data-testid="claim-vendor-block"' not in html


def test_vendor_response_deadline_and_scope_on_the_owner_side(app_module, db_session, client):
    owner = _user(app_module, db_session, "resp-owner@example.com")
    claimant = _user(app_module, db_session, "resp-claimant@example.com")
    project = _project(app_module, db_session, owner)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()

    _login(client, owner)
    owner_html = client.get("/ownership").get_data(as_text=True)
    assert f'data-claim-deadline="{claim.id}"' in owner_html
    assert "the ScanStory team can review the request directly" in owner_html
    assert "Agree to hand over" in owner_html

    # An unrelated account never sees the response controls.
    outsider = _user(app_module, db_session, "resp-outsider@example.com")
    _login(client, outsider)
    outsider_html = client.get("/ownership").get_data(as_text=True)
    assert f'data-claim-deadline="{claim.id}"' not in outsider_html
    assert "Agree to hand over" not in outsider_html


def test_claimant_copy_is_truthful_at_every_review_stage(app_module, db_session, client):
    owner = _user(app_module, db_session, "stage-owner@example.com")
    vendor = _user(app_module, db_session, "stage-vendor@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, "stage-claimant@example.com")
    project = _project(app_module, db_session, owner, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="mine")
    db_session.commit()

    _login(client, claimant)
    open_html = client.get("/ownership").get_data(as_text=True)
    assert 'data-claim-explainer="awaiting_response"' in open_html
    assert "Ownership has not changed" in open_html

    app_module.respond_to_project_ownership_claim(claim, owner, False, response_note="no")
    db_session.commit()
    review_html = client.get("/ownership").get_data(as_text=True)
    assert 'data-claim-explainer="pending_admin_review"' in review_html
    assert "now with the ScanStory team for review" in review_html
    # Nothing at any pre-completion stage claims the handover happened.
    assert "Ownership is now transferred" not in review_html
