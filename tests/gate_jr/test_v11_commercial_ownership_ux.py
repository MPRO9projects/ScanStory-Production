"""V1.1 Experience UX - business/vendor identity, ownership & coverage display,
reusable project-slot capacity, standalone ScanStory renewal, public content
reporting and the admin moderation surface.

These are structural/integration checks over rendered pages plus the JSON
endpoints those pages drive. Two invariants are load-bearing and get their own
tests rather than being implied:

  * no raw domain enum ever reaches an end-user screen (the label maps must
    cover every value the models accept, exactly);
  * every commercial number on screen comes from the server summary, and no
    price or pack size is hard-coded in a template.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


TEMPLATES = Path("templates")


def read_template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8", errors="ignore")


def as_vendor(db_session, user):
    user.account_type = "BUSINESS_VENDOR"
    db_session.commit()
    return user


def make_user(app_module, db_session, email, **kwargs):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=kwargs.pop("limit", 3),
        subscribed_scan_limit=100,
        projects_used=0,
        scans_used=0,
        **kwargs,
    )
    db_session.add(user)
    db_session.commit()
    return user


def make_catalog(app_module, db_session, code, addon_type, **deltas):
    item = app_module.AddonCatalog(
        code=code,
        name=code.replace("_", " ").title(),
        addon_type=addon_type,
        unit_amount=deltas.pop("unit_amount", 499.0),
        currency="INR",
        project_delta=deltas.get("project_delta"),
        validity_days_delta=deltas.get("validity_days_delta"),
        scan_delta=deltas.get("scan_delta"),
        is_active=deltas.pop("is_active", True),
        is_commercially_available=deltas.pop("is_commercially_available", True),
    )
    db_session.add(item)
    db_session.commit()
    return item


# ===========================================================================
# A. Label maps - the only place a raw enum becomes words
# ===========================================================================
def test_account_type_labels_cover_every_account_type(app_module):
    from models import USER_ACCOUNT_TYPES

    assert set(app_module.ACCOUNT_TYPE_LABELS) == USER_ACCOUNT_TYPES
    assert app_module.ACCOUNT_TYPE_LABELS["BUSINESS_VENDOR"] == "Business / Vendor"
    assert app_module.ACCOUNT_TYPE_LABELS["INDIVIDUAL"] == "Individual"


def test_transfer_status_labels_cover_every_transfer_status(app_module):
    from models import PROJECT_TRANSFER_STATUSES

    assert set(app_module.PROJECT_TRANSFER_STATUS_LABELS) == PROJECT_TRANSFER_STATUSES
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["PENDING_ACCEPTANCE"] == "Waiting for recipient"
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["PENDING_CAPACITY"] == (
        "Recipient needs an available project slot"
    )
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["COMPLETED"] == "Ownership transferred"
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["CANCELLED"] == "Transfer cancelled"
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["EXPIRED"] == "Transfer expired"
    assert app_module.PROJECT_TRANSFER_STATUS_LABELS["DISPUTED"] == "Transfer under review"


def test_claim_status_labels_cover_every_claim_status_and_never_say_take_ownership(app_module):
    from models import PROJECT_CLAIM_STATUSES

    assert set(app_module.PROJECT_CLAIM_STATUS_LABELS) == PROJECT_CLAIM_STATUSES
    joined = " ".join(app_module.PROJECT_CLAIM_STATUS_LABELS.values()).lower()
    assert "take ownership" not in joined


def test_coverage_source_labels_cover_every_source_type(app_module):
    from models import PROJECT_SERVICE_COVERAGE_SOURCE_TYPES

    assert set(app_module.PROJECT_COVERAGE_SOURCE_LABELS) == PROJECT_SERVICE_COVERAGE_SOURCE_TYPES


def test_report_label_maps_match_backend_enums_exactly(app_module):
    assert set(app_module.CONTENT_REPORT_REASON_LABELS) == app_module.CONTENT_REPORT_REASONS
    assert set(app_module.CONTENT_REPORT_STATUS_LABELS) == app_module.CONTENT_REPORT_STATUSES
    assert set(app_module.CONTENT_REPORT_ACTION_LABELS) == app_module.CONTENT_REPORT_ACTIONS
    assert app_module.CONTENT_REPORT_REASON_LABELS["EXPLICIT_OR_INAPPROPRIATE"] == (
        "Explicit or inappropriate content"
    )
    assert app_module.CONTENT_REPORT_REASON_LABELS["COPYRIGHT_OR_IP"] == (
        "Copyright or intellectual property"
    )


# ===========================================================================
# B. Business / Vendor and Individual account UX
# ===========================================================================
def test_vendor_dashboard_shows_business_identity_without_raw_enum(client, db_session, login_user):
    as_vendor(db_session, login_user)
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Business / Vendor Account" in body
    assert "BUSINESS_VENDOR" not in body


def test_individual_dashboard_has_no_vendor_badge(client, login_user):
    body = client.get("/dashboard").get_data(as_text=True)
    assert "Business / Vendor Account" not in body


def test_profile_shows_account_type_readonly_for_both_types(client, db_session, login_user):
    body = client.get("/profile").get_data(as_text=True)
    assert "Account Type" in body
    assert "Individual" in body

    as_vendor(db_session, login_user)
    vendor_body = client.get("/profile").get_data(as_text=True)
    assert "Business / Vendor" in vendor_body
    assert "BUSINESS_VENDOR" not in vendor_body


def test_profile_offers_no_client_side_account_type_switch(app_module):
    """No backend endpoint changes User.account_type, so no control may claim to."""
    html = read_template("user/profile.html")
    assert 'name="account_type"' not in html
    assert "account_type_label(user)" in html
    # And there is genuinely no such route to wire one to.
    rules = {str(rule) for rule in app_module.app.url_map.iter_rules()}
    assert not [r for r in rules if "account-type" in r or "account_type" in r]


# ===========================================================================
# C. Creator: for myself / for a customer
# ===========================================================================
def test_vendor_creator_sees_created_for_choice(client, db_session, login_user):
    as_vendor(db_session, login_user)
    body = client.get("/create-project").get_data(as_text=True)
    assert "Creating this ScanStory for" in body
    assert 'name="created_for"' in body
    assert "My business / myself" in body
    assert "A customer" in body


def test_individual_creator_never_sees_created_for_choice(client, login_user):
    body = client.get("/create-project").get_data(as_text=True)
    assert "Creating this ScanStory for" not in body
    assert 'id="createdForGroup"' not in body
    # No selectable control exists; the tiny JS reader that defaults to "self"
    # is unconditional and harmless.
    assert 'type="radio" name="created_for"' not in body


def test_vendor_review_step_shows_created_for_and_owner(client, db_session, login_user):
    as_vendor(db_session, login_user)
    body = client.get("/create-project").get_data(as_text=True)
    assert "recapCreatedFor" in body
    assert "recapOwner" in body
    # Owner is the real signed-in account, not an internal id.
    assert login_user.email in body
    assert f'>{login_user.id}<' not in body.split("recapOwner")[1][:120]


def test_creator_customer_option_never_claims_a_handover_happened(client, db_session, login_user):
    as_vendor(db_session, login_user)
    body = client.get("/create-project").get_data(as_text=True)
    assert "still created under your account" in body
    assert "transferred to" not in body.lower().replace("transferred to your customer", "")


def test_creator_three_step_wizard_and_primary_cta_not_regressed(client, login_user):
    """Prior checkpoint invariants: 3 steps, Story Name first, Create ScanStory CTA."""
    body = client.get("/create-project").get_data(as_text=True)
    for label in ("Details", "Content", "Review"):
        assert f'wizard-progress-label">{label}<' in body
    assert 'data-wizard-progress="4"' not in body
    assert body.index('for="projectName"') < body.index("Choose Experience Type")
    assert "Create ScanStory" in body


# ===========================================================================
# D. Ownership, transfer and claim display
# ===========================================================================
def _preview(client, project):
    return client.get(f"/project/{project.id}/preview").get_data(as_text=True)


def test_project_detail_shows_owner_relationship(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    body = _preview(client, project)
    assert "Ownership" in body
    assert "Owner" in body
    assert "You" in body


def test_project_detail_shows_managed_by_and_customer(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    vendor = as_vendor(db_session, make_user(app_module, db_session, "vendor-mgr@example.com"))
    customer = make_user(app_module, db_session, "beneficiary@example.com")
    project.manager_vendor_user_id = vendor.id
    project.beneficiary_user_id = customer.id
    db_session.commit()

    body = _preview(client, project)
    assert "Managed by" in body
    assert vendor.full_name in body
    assert "Customer" in body
    assert customer.full_name in body


def _transfer(app_module, db_session, project, status, recipient):
    transfer = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=project.owner_user_id,
        from_owner_user_id=project.owner_user_id,
        to_user_id=recipient.id,
        status=status,
    )
    db_session.add(transfer)
    db_session.commit()
    return transfer


def test_pending_acceptance_transfer_is_shown_without_claiming_completion(
    app_module, client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    recipient = make_user(app_module, db_session, "recipient-a@example.com")
    _transfer(app_module, db_session, project, "PENDING_ACCEPTANCE", recipient)

    body = _preview(client, project)
    assert "Waiting for recipient" in body
    assert "Ownership transferred" not in body
    assert "PENDING_ACCEPTANCE" not in body
    # The real rule: the sender's slot is only released on completion.
    assert "only released when the transfer completes" in body


def test_pending_capacity_transfer_never_hides_or_cancels_the_project(
    app_module, client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    recipient = make_user(app_module, db_session, "recipient-b@example.com")
    _transfer(app_module, db_session, project, "PENDING_CAPACITY", recipient)

    body = _preview(client, project)
    assert "Recipient needs an available project slot" in body
    assert "PENDING_CAPACITY" not in body
    assert "Nothing has been cancelled or moved" in body
    assert "keeps the same" in body  # QR is not mutated

    # The project itself is untouched: still active, still owned by the sender.
    db_session.refresh(project)
    assert project.is_active is True
    assert app_module.project_current_owner_user_id(project) == login_user.id

    # And it is still listed, not hidden.
    listing = client.get("/projects").get_data(as_text=True)
    assert project.name in listing


def test_completed_transfer_reads_as_transferred(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    recipient = make_user(app_module, db_session, "recipient-c@example.com")
    transfer = _transfer(app_module, db_session, project, "PENDING_ACCEPTANCE", recipient)
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.utcnow()
    db_session.commit()

    body = _preview(client, project)
    assert "Ownership transferred" in body
    assert "coverage bought for this ScanStory" in body.replace("Coverage", "coverage")


def test_claim_display_uses_review_wording_never_take_ownership(
    app_module, client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    claimant = make_user(app_module, db_session, "claimant@example.com")
    claim = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=claimant.id,
        current_owner_user_id=project.owner_user_id,
        status="PENDING_ADMIN_REVIEW",
    )
    db_session.add(claim)
    db_session.commit()

    body = _preview(client, project)
    assert "Ownership review requests" in body
    assert "Waiting for the ScanStory team to review" in body
    assert "checked by a person before ownership can change" in body
    assert "Take ownership" not in body
    assert "PENDING_ADMIN_REVIEW" not in body


def test_project_card_badges_use_labels_not_enums(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    recipient = make_user(app_module, db_session, "recipient-card@example.com")
    _transfer(app_module, db_session, project, "PENDING_CAPACITY", recipient)

    body = client.get("/projects").get_data(as_text=True)
    assert "Recipient needs an available project slot" in body
    assert "PENDING_CAPACITY" not in body


# ===========================================================================
# E. Project Slots (PROJECT_CAPACITY)
# ===========================================================================
def test_capacity_panel_renders_every_number_from_the_server_summary(client, db_session, login_user):
    login_user.subscribed_project_limit = 3
    login_user.projects_used = 1
    db_session.commit()

    body = client.get("/profile").get_data(as_text=True)
    assert "Project Slots" in body
    for label in ("Plan slots", "Purchased slots", "Total slots", "Used", "Available"):
        assert label in body
    assert 'data-capacity="base_project_limit"' in body
    assert 'data-capacity="projects_remaining"' in body


def test_purchased_slots_appear_in_the_panel_after_fulfilment(app_module, client, db_session, login_user):
    login_user.subscribed_project_limit = 3
    db_session.commit()
    item = make_catalog(app_module, db_session, "CAP_5_UI", "PROJECT_CAPACITY", project_delta=5)
    purchase = app_module.AddonPurchase(
        order_id="ADDON_UI_CAP_1",
        user_id=login_user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
    )
    db_session.add(purchase)
    db_session.commit()
    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    db_session.commit()

    summary = app_module.project_capacity_summary(app_module.User.query.get(login_user.id))
    assert summary["purchased_project_capacity"] == 5
    assert summary["base_project_limit"] == 3
    assert summary["effective_project_limit"] == 8

    body = client.get("/profile").get_data(as_text=True)
    assert ">8<" in body.replace(" ", "").replace("\n", "")


def test_capacity_panel_hardcodes_no_price_or_pack_size(app_module):
    html = read_template("user/profile.html")
    capacity_block = html.split("Project Slots", 1)[1].split("</section>", 1)[0]
    for forbidden in ("₹", "Rs.", "INR", "+1 slot", "+5 slot", "+10 slot"):
        assert forbidden not in capacity_block
    # Options are rendered from the real catalog response by the shared script.
    assert "data-ss-addon-list" in capacity_block


def test_capacity_wording_is_reusable_slots_never_credits(client, login_user):
    """Checked against the RENDERED page, so a source-comment can't satisfy it."""
    body = client.get("/profile").get_data(as_text=True)
    capacity_block = body.split("Project Slots", 1)[1].split("</section>", 1)[0]
    assert "credit" not in capacity_block.lower()
    assert "reusable" in capacity_block.lower()
    assert "returns to your pool" in capacity_block


def test_catalog_endpoint_only_lists_purchasable_items(app_module, client, db_session, login_user):
    live = make_catalog(app_module, db_session, "CAP_LIVE", "PROJECT_CAPACITY", project_delta=1)
    make_catalog(app_module, db_session, "CAP_OFF", "PROJECT_CAPACITY", project_delta=1, is_active=False)
    make_catalog(
        app_module, db_session, "CAP_NOSALE", "PROJECT_CAPACITY",
        project_delta=1, is_commercially_available=False,
    )

    payload = client.get("/api/addons/catalog").get_json()
    codes = {item["code"] for item in payload["addons"]}
    assert live.code in codes
    assert "CAP_OFF" not in codes
    assert "CAP_NOSALE" not in codes


def test_capacity_purchase_is_account_level_and_rejects_a_project_target(
    app_module, client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    item = make_catalog(app_module, db_session, "CAP_ACCT", "PROJECT_CAPACITY", project_delta=1)
    response = client.post(
        "/api/addons/orders",
        json={"catalog_id": item.id, "quantity": 1, "project_id": project.id},
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "PROJECT_TARGET_INVALID"


# ===========================================================================
# F. ScanStory Coverage & renewal
# ===========================================================================
def test_renewal_section_is_offered_when_eligible(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    body = _preview(client, project)
    assert "ScanStory Coverage" in body
    assert "Extend ScanStory Coverage" in body
    assert "Extend Subscription" not in body
    assert 'id="renewalSection"' in body
    assert "data-ss-addon-list" in body


def test_renewal_copy_never_implies_the_account_plan_changes(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    body = _preview(client, project)
    assert "does not change your account plan or its renewal date" in body


def test_legacy_indefinite_coverage_blocks_the_purchase_cta(
    app_module, client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    app_module.add_project_service_coverage(
        project, "LEGACY_COMPATIBILITY", coverage_end=None, reason="backfill"
    )
    db_session.commit()

    coverage = app_module.project_coverage_summary(project)
    assert coverage["renewal_eligible"] is False
    assert coverage["renewal_blocked_code"] == "COVERAGE_ALREADY_INDEFINITE"

    body = _preview(client, project)
    assert "does not currently require standalone renewal" in body
    assert 'data-ss-addon-list' not in body  # no enabled purchase surface at all
    assert "COVERAGE_ALREADY_INDEFINITE" not in body


def test_suspended_project_is_not_shown_as_expired_and_offers_no_renewal(
    client, db_session, login_user, project_with_pair
):
    project, _pair = project_with_pair
    project.is_active = False
    db_session.commit()

    body = _preview(client, project)
    assert "Suspended" in body
    assert "Coverage expired" not in body
    assert "Extend ScanStory Coverage" not in body
    assert "have not been deleted" in body
    assert "will not lift a suspension" in body


def test_coverage_valid_until_comes_from_the_summary(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    login_user.subscription_expires_at = datetime(2027, 3, 5)
    db_session.commit()

    coverage = app_module.project_coverage_summary(project)
    assert coverage["effective_coverage_until"].startswith("2027-03-05")

    body = _preview(client, project)
    assert "05 Mar 2027" in body


def test_expired_coverage_reads_as_expired_not_suspended(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    login_user.subscription_status = "expired"
    login_user.subscription_expires_at = datetime.utcnow() - timedelta(days=2)
    db_session.commit()

    body = _preview(client, project)
    assert "Coverage expired" in body
    assert "Suspended" not in body


def test_renewal_order_requires_the_project_id(app_module, client, db_session, login_user, project_with_pair):
    project, _pair = project_with_pair
    item = make_catalog(
        app_module, db_session, "REN_365", "PROJECT_SERVICE_COVERAGE", validity_days_delta=365
    )
    response = client.post("/api/addons/orders", json={"catalog_id": item.id, "quantity": 1})
    assert response.status_code == 404
    assert response.get_json()["code"] == "PROJECT_NOT_FOUND"


# ===========================================================================
# G. Public content reporting
# ===========================================================================
def test_scanner_exposes_a_report_action_without_touching_the_lens(client, project_with_pair):
    project, _pair = project_with_pair
    body = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    assert 'id="reportOpenBtn"' in body
    assert 'id="reportSheet"' in body
    # The report sheet must not be injected into the camera surface or the
    # in-lens recovery panels.
    lens = body.split('<div class="wrap" id="wrap">', 1)[1].split("</body>", 1)[0]
    assert 'id="reportSheet"' not in lens.split('id="fallbackPanel"', 1)[0]


def test_scanner_report_reasons_match_the_backend_codes_exactly(app_module, client, project_with_pair):
    project, _pair = project_with_pair
    body = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    for code, label in app_module.CONTENT_REPORT_REASON_LABELS.items():
        assert f'value="{code}"' in body
        assert label in body
    assert f'maxlength="{app_module.CONTENT_REPORT_DETAILS_MAX}"' in body


def test_anonymous_viewer_can_submit_a_report(app_module, client, db_session, project_with_pair):
    project, _pair = project_with_pair
    response = client.post(
        f"/api/projects/{project.id}/report",
        json={"reason": "SPAM", "details": "Repeated advertising."},
    )
    assert response.status_code == 201
    report = app_module.ContentReport.query.filter_by(project_id=project.id).first()
    assert report is not None
    assert report.reporter_user_id is None
    assert report.status == "OPEN"
    # Reporting never acts on the project.
    db_session.refresh(project)
    assert project.is_active is True


def test_report_success_copy_never_promises_removal_or_a_ban(app_module, client, project_with_pair):
    project, _pair = project_with_pair
    body = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    assert "Report submitted for review." in body
    for forbidden in ("Content removed", "Creator banned", "Violation confirmed"):
        assert forbidden not in body
    assert "nothing is removed automatically" in body.lower()


def test_report_rate_limit_is_surfaced_as_readable_copy(client, project_with_pair):
    project, _pair = project_with_pair
    body = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    assert "sent several reports already" in body
    # The report block itself never prints a backend code. ("RATE_LIMITED"
    # appears elsewhere in this page as pre-existing scanner telemetry.)
    report_block = body.split("V1.1 public content report", 1)[1]
    assert "RATE_LIMITED" not in report_block


def test_report_rejects_an_unknown_reason(client, project_with_pair):
    project, _pair = project_with_pair
    response = client.post(f"/api/projects/{project.id}/report", json={"reason": "I_DISLIKE_IT"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "INVALID_REASON"


# ===========================================================================
# H. Admin moderation
# ===========================================================================
def _report_row(app_module, db_session, project, reason="SPAM"):
    report = app_module.ContentReport(project_id=project.id, reason=reason, status="OPEN")
    db_session.add(report)
    db_session.commit()
    return report


def test_moderation_page_requires_admin_and_renders_the_queue_shell(
    app_module, client, db_session, project_with_pair, admin
):
    project, _pair = project_with_pair
    _report_row(app_module, db_session, project)

    anonymous = client.get("/admin/moderation")
    assert anonymous.status_code in (302, 401, 403)

    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    page = client.get("/admin/moderation")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Content Reports" in body
    assert "reportRows" in body


def test_moderation_queue_json_powers_the_page_and_names_the_project(
    app_module, client, db_session, project_with_pair, admin
):
    project, _pair = project_with_pair
    _report_row(app_module, db_session, project, reason="COPYRIGHT_OR_IP")
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    payload = client.get("/admin/reports").get_json()
    assert payload["success"] is True
    assert payload["reports"][0]["project_name"] == project.name
    assert payload["reports"][0]["reason"] == "COPYRIGHT_OR_IP"

    filtered = client.get("/admin/reports?status=OPEN").get_json()
    assert len(filtered["reports"]) == 1
    assert client.get("/admin/reports?status=DISMISSED").get_json()["reports"] == []


def test_moderation_transitions_under_review_then_dismissed(
    app_module, client, db_session, project_with_pair, admin
):
    project, _pair = project_with_pair
    report = _report_row(app_module, db_session, project)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    assert client.post(f"/admin/reports/{report.id}/review", json={"status": "UNDER_REVIEW"}).status_code == 200
    assert client.post(
        f"/admin/reports/{report.id}/review",
        json={"status": "DISMISSED", "resolution_reason": "Not a violation."},
    ).status_code == 200
    db_session.refresh(report)
    db_session.refresh(project)
    assert report.status == "DISMISSED"
    assert project.is_active is True


def test_project_suspension_confirmation_states_nothing_is_deleted(app_module):
    html = read_template("admin/moderation.html")
    assert "publicly unavailable" in html
    assert "not</strong> deleted" in html
    assert "QR code and project identity stay intact" in html
    assert "admin activity log" in html
    assert "window.confirm" in html


def test_moderation_ui_offers_no_delete_ban_or_refund_action(app_module):
    html = read_template("admin/moderation.html").lower()
    for forbidden in ("delete content", "ban creator", "ban the creator", "issue refund", "refund payment"):
        assert forbidden not in html
    # Only the actions the backend actually accepts are selectable.
    assert set(app_module.CONTENT_REPORT_ACTIONS) == {
        "NONE", "PROJECT_SUSPENDED", "CREATOR_CONTACT_REQUIRED", "LEGAL_REVIEW_REQUIRED", "OTHER",
    }


def test_moderation_controls_hidden_without_manage_permission(app_module, client, db_session, project_with_pair, admin, monkeypatch):
    project, _pair = project_with_pair
    _report_row(app_module, db_session, project)
    reduced = set(app_module.ADMIN_ROLE_PERMISSIONS["admin"]) - {"admin.reports.manage"}
    monkeypatch.setitem(app_module.ADMIN_ROLE_PERMISSIONS, "admin", reduced)
    admin.role = "admin"
    db_session.commit()

    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id
    body = client.get("/admin/moderation").get_data(as_text=True)
    assert "read-only access to content reports" in body
    assert "const CAN_MANAGE = false" in body

    denied = client.post(f"/admin/reports/1/review", json={"status": "DISMISSED"})
    assert denied.status_code in (302, 401, 403)


def test_moderation_permission_codes_are_the_real_ones(app_module):
    for role in ("admin", "superadmin"):
        assert "admin.reports.view" in app_module.ADMIN_ROLE_PERMISSIONS[role]
        assert "admin.reports.manage" in app_module.ADMIN_ROLE_PERMISSIONS[role]
    html = read_template("admin/base.html")
    assert "admin_can('admin.reports.view')" in html


# ===========================================================================
# I. Refund boundary, accessibility, responsive and regression guards
# ===========================================================================
def test_no_user_facing_refund_action_exists_anywhere(app_module):
    """A 'refunded' payment status exists for history; a refund FEATURE does not."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in ("request refund", "cancel & refund", "cancel and refund", "issue refund", "process refund"):
            if phrase in text:
                offenders.append((str(path), phrase))
    assert offenders == []
    # The one refund control in the admin package is explicitly disabled.
    payment_html = read_template("admin/view_payment.html")
    assert "Refund unavailable" in payment_html
    assert "disabled" in payment_html.split("Refund unavailable")[0][-400:]
    # And no route exposes one.
    rules = {str(rule) for rule in app_module.app.url_map.iter_rules()}
    assert not [r for r in rules if "refund" in r.lower()]


@pytest.mark.parametrize(
    "template",
    ["user/project_preview.html", "user/profile.html", "user/scanner.html", "admin/moderation.html"],
)
def test_new_surfaces_use_real_buttons_and_labelled_regions(template):
    html = read_template(template)
    assert "<div onclick" not in html.replace(" ", "")
    assert "aria-live" in html or "role=" in html


def test_report_sheet_is_keyboard_operable_and_escapable():
    html = read_template("user/scanner.html")
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert "event.key === 'Escape'" in html
    assert "lastFocused" in html  # focus returns to the trigger on close
    assert 'role="radiogroup"' in html


def test_new_touch_targets_meet_the_minimum_size():
    css = Path("static/css/design-system.css").read_text(encoding="utf-8")
    for selector in (".ss-report-trigger {", ".ss-addon-buy {", ".ss-sheet-reason {", ".ss-sheet-actions button {"):
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "min-height: 44px" in block or "min-height:44px" in block


def test_new_layouts_avoid_fixed_widths_that_would_overflow_at_320px():
    css = Path("static/css/design-system.css").read_text(encoding="utf-8")
    tail = css.split("V1.1 - ownership, coverage, capacity and moderation surfaces", 1)[1]
    # No px-based `width:` on a block that would force a 320px viewport wider.
    assert "width: 100%" in tail
    assert "min-width: 0" in tail
    for offender in ("width: 480px", "width: 600px", "min-width: 400px"):
        assert offender not in tail


def test_status_is_never_conveyed_by_colour_alone():
    """Every chip tone in the panel carries a word; the tone only tints it."""
    html = read_template("user/project_preview.html")
    for tone_marker in ('data-tone="ok"', 'data-tone="wait"', 'data-tone="stop"'):
        assert tone_marker in html
    assert "Suspended" in html and "Coverage expired" in html and "Active" in html


def test_scanner_recovery_and_playback_contract_not_regressed(client, project_with_pair):
    """Guards the prior checkpoints: the report addition must not have moved
    or removed any existing in-lens recovery control."""
    project, _pair = project_with_pair
    body = client.get(f"/scanner/{project.id}").get_data(as_text=True)
    for marker in (
        'id="fallbackPanel"',
        'id="fallbackRetryBtn"',
        'id="fallbackWatchBtn"',
        'id="recognitionHelpPanel"',
        'id="recognitionContinueBtn"',
        'id="targetGuide"',
        'id="startCameraBtn"',
        'id="playbackModeChoice"',
    ):
        assert marker in body
    assert "Retry Camera" in body
    assert "Watch video instead" in body


def test_no_scanner_algorithm_symbols_were_touched():
    """The report UI is appended after every detection script and shares no
    identifier with it."""
    html = read_template("user/scanner.html")
    report_block = html.split("V1.1 public content report", 1)[1]
    for algorithm_symbol in ("ORB", "findHomography", "RANSAC", "calcOpticalFlow", "warpPerspective", "detectAndCompute"):
        assert algorithm_symbol not in report_block
