"""Wave 4 tests: vendor/business ownership transfer + claim backend.

Covers the governed transfer lifecycle, the two-dimension capacity gate
(project slots AND account storage), non-destructive PENDING_CAPACITY and its
recoverable retry, MediaObject responsibility movement, the claim workflow with
vendor response and Admin adjudication, the HTTP surface's authorization and
CSRF, and idempotence.

Focused scope by policy: the full suite and the full PostgreSQL certification
lane are the project lead's, run once after this wave merges.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _user(app_module, db_session, plan, email, *, account_type="INDIVIDUAL",
          limit=5, used=0, storage_used=0):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        account_type=account_type,
        subscription_id=plan.id,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=limit,
        subscribed_scan_limit=100,
        projects_used=used,
        scans_used=0,
        storage_used_bytes=storage_used,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _project(app_module, db_session, owner, *, creator=None, vendor=None, beneficiary=None, name="Wave4 Project"):
    project = app_module.Project(
        name=name,
        owner_user_id=owner.id,
        created_by_user_id=(creator or owner).id,
        current_owner_user_id=owner.id,
        manager_vendor_user_id=vendor.id if vendor else None,
        beneficiary_user_id=beneficiary.id if beneficiary else None,
        user_project_index=1,
        scanner_url="/scanner/w4",
        qr_code_filename="project_w4_main.png",
        qr_code_path="/qr/project_w4_main.png",
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _media(app_module, db_session, project, owner, image_bytes=10, video_bytes=20):
    import storage_accounting as sa

    sa.record_media_object(f"user/images/{project.id}_0.jpg", image_bytes, "trigger_image",
                           owner_user_id=owner.id, project_id=project.id)
    sa.record_media_object(f"user/videos/{project.id}_0.mp4", video_bytes, "video",
                           owner_user_id=owner.id, project_id=project.id)
    owner.storage_used_bytes = int(owner.storage_used_bytes or 0) + image_bytes + video_bytes
    db_session.commit()
    return image_bytes + video_bytes


def _storage_allowance(app_module, db_session, user, allowance_bytes):
    plan = app_module.SubscriptionPlan.query.get(user.subscription_id)
    plan.base_storage_bytes = allowance_bytes
    db_session.commit()
    return plan


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user.id


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess.clear()
        sess["admin_id"] = admin.id


# ===========================================================================
# Transfer lifecycle
# ===========================================================================
def test_vendor_created_customer_project_transfer_preserves_creator_and_beneficiary(
    app_module, db_session, plan
):
    vendor = _user(app_module, db_session, plan, "vendor-a@example.com", account_type="BUSINESS_VENDOR", used=1)
    customer = _user(app_module, db_session, plan, "customer-a@example.com")
    project = _project(app_module, db_session, vendor, vendor=vendor, beneficiary=customer)

    transfer = app_module.initiate_project_ownership_transfer(
        project, vendor, customer, retain_vendor_management=True, reason="handover"
    )
    db_session.commit()
    assert transfer.status == "PENDING_ACCEPTANCE"

    app_module.accept_project_ownership_transfer(transfer, acting_user=customer)
    db_session.commit()
    db_session.expire_all()

    project = app_module.Project.query.get(project.id)
    assert transfer.status == "COMPLETED"
    # Creator history is never rewritten; the vendor stays the creator.
    assert project.created_by_user_id == vendor.id
    assert project.current_owner_user_id == customer.id
    assert project.owner_user_id == customer.id
    assert project.manager_vendor_user_id == vendor.id
    assert project.beneficiary_user_id == customer.id
    assert app_module.User.query.get(vendor.id).projects_used == 0
    assert app_module.User.query.get(customer.id).projects_used == 1


def test_transfer_is_never_automatic_from_beneficiary_or_creator(app_module, db_session, plan):
    vendor = _user(app_module, db_session, plan, "vendor-b@example.com", account_type="BUSINESS_VENDOR")
    customer = _user(app_module, db_session, plan, "customer-b@example.com")
    project = _project(app_module, db_session, vendor, vendor=vendor, beneficiary=customer)
    db_session.commit()

    # A beneficiary alone changes nothing: ownership only moves through an
    # explicit, accepted transfer.
    assert app_module.project_current_owner_user_id(project) == vendor.id
    assert app_module.ProjectOwnershipTransfer.query.count() == 0


def test_unauthorized_user_cannot_initiate_or_accept_a_transfer(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-c@example.com")
    outsider = _user(app_module, db_session, plan, "outsider-c@example.com")
    project = _project(app_module, db_session, normal_user)

    with pytest.raises(PermissionError):
        app_module.initiate_project_ownership_transfer(project, outsider, recipient)

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()
    with pytest.raises(PermissionError):
        app_module.accept_project_ownership_transfer(transfer, acting_user=outsider)


def test_recipient_rejection_and_sender_cancellation_use_existing_states(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-d@example.com")
    project = _project(app_module, db_session, normal_user)

    rejected = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()
    with pytest.raises(PermissionError):
        app_module.reject_project_ownership_transfer(rejected, normal_user)
    app_module.reject_project_ownership_transfer(rejected, recipient, reason="not mine")
    db_session.commit()
    assert rejected.status == "CANCELLED"
    assert rejected.cancelled_at is not None
    assert any(e["action"] == "transfer_rejected" for e in app_module.ownership_audit_trail(rejected))

    cancelled = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()
    with pytest.raises(PermissionError):
        app_module.cancel_project_ownership_transfer(cancelled, acting_user=recipient)
    app_module.cancel_project_ownership_transfer(cancelled, acting_user=normal_user, reason="changed my mind")
    db_session.commit()
    assert cancelled.status == "CANCELLED"
    # Nothing moved on either terminal path.
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id


def test_expired_transfer_cannot_be_accepted(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-e@example.com")
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(
        project, normal_user, recipient, expires_at=datetime.utcnow() - timedelta(days=1)
    )
    db_session.commit()

    with pytest.raises(ValueError):
        app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "EXPIRED"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id


# ===========================================================================
# Capacity: BOTH dimensions
# ===========================================================================
def test_transfer_capacity_evaluates_project_slots_and_storage_together(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-f@example.com", limit=1, used=1)
    project = _project(app_module, db_session, normal_user)
    _media(app_module, db_session, project, normal_user, 10, 20)
    _storage_allowance(app_module, db_session, recipient, 5)
    db_session.commit()

    snapshot = app_module.evaluate_transfer_capacity(project, recipient)
    assert snapshot["storage_ok"] is False
    assert snapshot["project_slot_ok"] is False
    assert snapshot["project_bytes"] == 30
    assert snapshot["recipient_project_limit"] == 1


def test_insufficient_project_capacity_parks_transfer_without_moving_anything(
    app_module, db_session, plan, normal_user
):
    recipient = _user(app_module, db_session, plan, "recipient-g@example.com", limit=1, used=1)
    project = _project(app_module, db_session, normal_user)
    moved = _media(app_module, db_session, project, normal_user, 10, 20)
    normal_user.projects_used = 1
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    db_session.expire_all()

    transfer = app_module.ProjectOwnershipTransfer.query.get(transfer.id)
    assert transfer.status == "PENDING_CAPACITY"
    block = app_module.transfer_capacity_snapshot(transfer)
    assert block["project_slot_ok"] is False
    assert block["storage_ok"] is True
    assert block["project_bytes"] == moved
    # No partial movement of ownership, capacity, storage or media.
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id
    assert app_module.User.query.get(normal_user.id).projects_used == 1
    assert app_module.User.query.get(recipient.id).projects_used == 1
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == moved
    assert app_module.User.query.get(recipient.id).storage_used_bytes == 0
    assert all(o.owner_user_id == normal_user.id for o in app_module.MediaObject.query.all())


def test_insufficient_storage_parks_transfer_and_records_the_failing_dimension(
    app_module, db_session, plan, normal_user
):
    recipient = _user(app_module, db_session, plan, "recipient-h@example.com", limit=5, used=0)
    project = _project(app_module, db_session, normal_user)
    moved = _media(app_module, db_session, project, normal_user, 10, 20)
    _storage_allowance(app_module, db_session, recipient, 5)  # cannot absorb 30
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    db_session.expire_all()

    transfer = app_module.ProjectOwnershipTransfer.query.get(transfer.id)
    assert transfer.status == "PENDING_CAPACITY"
    block = app_module.transfer_capacity_snapshot(transfer)
    assert block["storage_ok"] is False
    assert block["project_bytes"] == moved
    # The storage check runs BEFORE the slot reservation, so no counter was
    # consumed and nothing has to be unwound.
    assert app_module.User.query.get(recipient.id).projects_used == 0
    assert app_module.User.query.get(recipient.id).storage_used_bytes == 0
    assert all(o.owner_user_id == normal_user.id for o in app_module.MediaObject.query.all())


def test_pending_capacity_transfer_completes_once_capacity_appears(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-i@example.com", limit=1, used=1)
    project = _project(app_module, db_session, normal_user)
    moved = _media(app_module, db_session, project, normal_user, 10, 20)
    normal_user.projects_used = 1
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    assert transfer.status == "PENDING_CAPACITY"

    # Recipient frees a slot. The SAME transfer row resumes - no re-initiation.
    recipient.projects_used = 0
    db_session.commit()
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    db_session.expire_all()

    assert app_module.ProjectOwnershipTransfer.query.count() == 1
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "COMPLETED"
    assert app_module.Project.query.get(project.id).current_owner_user_id == recipient.id
    # Sender's slot is freed only now, on real completion.
    assert app_module.User.query.get(normal_user.id).projects_used == 0
    assert app_module.User.query.get(recipient.id).projects_used == 1
    assert app_module.User.query.get(recipient.id).storage_used_bytes == moved
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0


# ===========================================================================
# Storage responsibility movement
# ===========================================================================
def test_completed_transfer_moves_media_ownership_exactly_once_and_deletes_nothing(
    app_module, db_session, plan, normal_user, project_with_pair
):
    project, pair = project_with_pair
    project.current_owner_user_id = normal_user.id
    project.created_by_user_id = normal_user.id
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    normal_user.projects_used = 1
    recipient = _user(app_module, db_session, plan, "recipient-j@example.com", limit=5)
    db_session.commit()

    image_path = Path(app_module.IMAGES_DIR) / pair.image_filename
    video_path = Path(app_module.VIDEOS_DIR) / pair.video_filename
    assert image_path.exists() and video_path.exists()

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()

    # A second attempt on the completed row must not move the ledger again.
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    db_session.expire_all()

    objects = app_module.MediaObject.query.all()
    assert len(objects) == 2
    assert all(o.owner_user_id == recipient.id for o in objects)
    assert sum(o.size_bytes for o in objects) == 30
    assert app_module.User.query.get(recipient.id).storage_used_bytes == 30
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0
    # Billing responsibility moved; the physical files never did.
    assert image_path.exists() and video_path.exists()


# ===========================================================================
# Idempotence / concurrency
# ===========================================================================
def test_repeated_acceptance_never_double_accounts(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-k@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    normal_user.projects_used = 1
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    for _ in range(3):
        app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
        db_session.commit()
    db_session.expire_all()

    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "COMPLETED"
    assert app_module.User.query.get(recipient.id).projects_used == 1
    assert app_module.User.query.get(normal_user.id).projects_used == 0


def test_status_gate_rejects_a_transition_whose_row_already_moved_on(app_module, db_session, plan, normal_user):
    """The conditional UPDATE, not the in-memory read, decides. A duplicate
    request holding a stale status matches zero rows and no-ops."""
    recipient = _user(app_module, db_session, plan, "recipient-l@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    assert app_module._transition_transfer(transfer, ("PENDING_ACCEPTANCE",), "CANCELLED") is True
    db_session.commit()
    # Second caller, same intent, row already moved: no second effect.
    assert app_module._transition_transfer(transfer, ("PENDING_ACCEPTANCE",), "EXPIRED") is False


def test_transfer_refuses_to_complete_if_the_owner_changed_underneath_it(app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-m@example.com", limit=5)
    other = _user(app_module, db_session, plan, "other-m@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    project.current_owner_user_id = other.id
    project.owner_user_id = other.id
    db_session.commit()

    with pytest.raises(ValueError):
        app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)


# ===========================================================================
# Claims
# ===========================================================================
def test_claim_is_evidence_not_ownership_and_dedupes(app_module, db_session, plan, normal_user):
    claimant = _user(app_module, db_session, plan, "claimant-n@example.com")
    project = _project(app_module, db_session, normal_user)

    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="invoice 42")
    duplicate = app_module.create_project_ownership_claim(project, claimant, evidence_summary="again")
    db_session.commit()

    assert duplicate.id == claim.id
    assert claim.status == "OPEN"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id
    assert app_module.ProjectOwnershipTransfer.query.count() == 0
    assert any(e["action"] == "claim_submitted" for e in app_module.ownership_audit_trail(claim))


def test_vendor_response_scope_is_backend_enforced(app_module, db_session, plan, normal_user):
    vendor = _user(app_module, db_session, plan, "vendor-o@example.com", account_type="BUSINESS_VENDOR")
    unrelated_vendor = _user(app_module, db_session, plan, "unrelated-o@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, plan, "claimant-o@example.com")
    project = _project(app_module, db_session, vendor, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="receipt")
    db_session.commit()

    assert app_module.user_can_respond_to_claim(unrelated_vendor, claim) is False
    assert app_module.user_can_respond_to_claim(claimant, claim) is False
    with pytest.raises(PermissionError):
        app_module.respond_to_project_ownership_claim(claim, unrelated_vendor, True)
    with pytest.raises(PermissionError):
        app_module.respond_to_project_ownership_claim(claim, claimant, True)


def test_vendor_acceptance_opens_a_transfer_and_refusal_escalates_to_admin(app_module, db_session, plan):
    vendor = _user(app_module, db_session, plan, "vendor-p@example.com", account_type="BUSINESS_VENDOR")
    claimant = _user(app_module, db_session, plan, "claimant-p@example.com", limit=5)
    project = _project(app_module, db_session, vendor, vendor=vendor)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()

    claim, transfer = app_module.respond_to_project_ownership_claim(claim, vendor, True, response_note="agreed")
    db_session.commit()
    assert claim.status == "APPROVED_BY_VENDOR"
    assert transfer.status == "PENDING_ACCEPTANCE"
    # Agreement is CONSENT, not a transfer: ownership has not moved.
    assert app_module.Project.query.get(project.id).current_owner_user_id == vendor.id

    # Completing the transfer closes the claim exactly once.
    app_module.accept_project_ownership_transfer(transfer, acting_user=claimant)
    db_session.commit()
    db_session.expire_all()
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == "TRANSFER_COMPLETED"

    # A refusal never closes a claim unilaterally.
    other_claimant = _user(app_module, db_session, plan, "claimant-p2@example.com")
    other_project = _project(app_module, db_session, vendor, vendor=vendor, name="Second")
    other_claim = app_module.create_project_ownership_claim(other_project, other_claimant)
    db_session.commit()
    other_claim, no_transfer = app_module.respond_to_project_ownership_claim(other_claim, vendor, False)
    db_session.commit()
    assert other_claim.status == "PENDING_ADMIN_REVIEW"
    assert no_transfer is None


def test_admin_approval_does_not_transfer_and_still_respects_capacity(app_module, db_session, plan, normal_user, admin):
    claimant = _user(app_module, db_session, plan, "claimant-q@example.com", limit=1, used=1)
    project = _project(app_module, db_session, normal_user)
    normal_user.projects_used = 1
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="proof")
    db_session.commit()

    claim, transfer = app_module.approve_project_ownership_claim_by_admin(claim, admin, "owner unreachable")
    db_session.commit()
    assert claim.status == "APPROVED_BY_ADMIN"
    assert claim.reviewed_by_admin_id == admin.id
    assert transfer.status == "PENDING_ACCEPTANCE"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id

    # An approved claim is still subject to the capacity gate.
    app_module.accept_project_ownership_transfer(transfer, acting_user=claimant)
    db_session.commit()
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "PENDING_CAPACITY"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id


def test_admin_rejection_and_repeated_resolution_are_safe(app_module, db_session, plan, normal_user, admin):
    claimant = _user(app_module, db_session, plan, "claimant-r@example.com")
    project = _project(app_module, db_session, normal_user)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()

    app_module.reject_project_ownership_claim_by_admin(claim, admin, "insufficient evidence")
    db_session.commit()
    assert claim.status == "REJECTED"
    assert claim.decision_reason == "insufficient evidence"

    with pytest.raises(ValueError):
        app_module.reject_project_ownership_claim_by_admin(claim, admin, "again")
    with pytest.raises(ValueError):
        app_module.approve_project_ownership_claim_by_admin(claim, admin, "flip flop")
    db_session.rollback()
    assert app_module.ProjectOwnershipTransfer.query.count() == 0


def test_dispute_preserves_the_current_owner_until_manual_resolution(app_module, db_session, plan, normal_user, admin):
    recipient = _user(app_module, db_session, plan, "recipient-s@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    app_module.mark_project_transfer_disputed(transfer, admin, "conflicting evidence")
    db_session.commit()
    assert transfer.status == "DISPUTED"
    # Frozen: the recipient cannot push it through while disputed.
    with pytest.raises(ValueError):
        app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id

    app_module.release_project_transfer_dispute(transfer, admin, "evidence resolved")
    db_session.commit()
    assert transfer.status == "PENDING_ACCEPTANCE"
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()
    assert transfer.status == "COMPLETED"


# ===========================================================================
# HTTP surface: authorization, enumeration, CSRF
# ===========================================================================
def test_transfer_http_flow_end_to_end(client, app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-t@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    normal_user.projects_used = 1
    db_session.commit()

    _login(client, normal_user)
    assert client.get("/ownership").status_code == 200
    response = client.post(
        f"/projects/{project.id}/transfer",
        data={"recipient_email": recipient.email},
        follow_redirects=False,
    )
    assert response.status_code == 302
    transfer = app_module.ProjectOwnershipTransfer.query.one()
    assert transfer.to_user_id == recipient.id

    _login(client, recipient)
    assert client.post(f"/ownership/transfers/{transfer.id}/accept").status_code == 302
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "COMPLETED"
    assert app_module.Project.query.get(project.id).current_owner_user_id == recipient.id


def test_transfer_and_claim_ids_cannot_be_enumerated(client, app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-u@example.com", limit=5)
    outsider = _user(app_module, db_session, plan, "outsider-u@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    claim = app_module.create_project_ownership_claim(project, recipient)
    db_session.commit()

    _login(client, outsider)
    # Being logged in with a valid numeric id is not authorization.
    assert client.post(f"/ownership/transfers/{transfer.id}/accept").status_code == 404
    assert client.post(f"/ownership/transfers/{transfer.id}/reject").status_code == 404
    assert client.post(f"/ownership/transfers/{transfer.id}/cancel").status_code == 404
    assert client.post(f"/ownership/claims/{claim.id}/respond", data={"decision": "accept"}).status_code == 404
    assert client.post(f"/ownership/claims/{claim.id}/cancel").status_code == 404
    assert client.post(f"/projects/{project.id}/transfer", data={"recipient_email": outsider.email}).status_code == 404

    # The sender is a party but is not the recipient: still no acceptance.
    _login(client, normal_user)
    assert client.post(f"/ownership/transfers/{transfer.id}/accept").status_code == 404
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "PENDING_ACCEPTANCE"


def test_state_changing_ownership_routes_require_csrf(client, app, app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-v@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    db_session.commit()

    _login(client, normal_user)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            f"/projects/{project.id}/transfer", data={"recipient_email": recipient.email}
        )
        assert response.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert app_module.ProjectOwnershipTransfer.query.count() == 0


def test_ownership_mutations_reject_insecure_get(client, app_module, db_session, plan, normal_user):
    recipient = _user(app_module, db_session, plan, "recipient-w@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    _login(client, recipient)
    assert client.get(f"/ownership/transfers/{transfer.id}/accept").status_code == 405
    _login(client, normal_user)
    assert client.get(f"/projects/{project.id}/transfer").status_code == 405


def test_normal_user_cannot_reach_admin_ownership_review(client, app_module, db_session, plan, normal_user, admin):
    claimant = _user(app_module, db_session, plan, "claimant-x@example.com")
    project = _project(app_module, db_session, normal_user)
    claim = app_module.create_project_ownership_claim(project, claimant)
    db_session.commit()

    _login(client, normal_user)
    # No admin session -> bounced to the admin login, never executed.
    assert client.post(f"/admin/ownership/claims/{claim.id}/approve").status_code == 302
    assert client.get("/admin/ownership").status_code == 302
    db_session.expire_all()
    assert app_module.ProjectOwnershipClaim.query.get(claim.id).status == "OPEN"


def test_admin_ownership_review_page_and_resolution(client, app_module, db_session, plan, normal_user, admin):
    claimant = _user(app_module, db_session, plan, "claimant-y@example.com", limit=5)
    project = _project(app_module, db_session, normal_user)
    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="proof")
    db_session.commit()

    _login_admin(client, admin)
    assert client.get("/admin/ownership").status_code == 200
    assert client.post(
        f"/admin/ownership/claims/{claim.id}/approve", data={"decision_reason": "verified"}
    ).status_code == 302
    db_session.expire_all()
    claim = app_module.ProjectOwnershipClaim.query.get(claim.id)
    assert claim.status == "APPROVED_BY_ADMIN"
    transfer = app_module.ProjectOwnershipTransfer.query.one()
    assert transfer.status == "PENDING_ACCEPTANCE"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id

    assert client.post(f"/admin/ownership/transfers/{transfer.id}/dispute").status_code == 302
    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.get(transfer.id).status == "DISPUTED"
    assert app_module.AdminActivity.query.filter_by(activity_type="ownership_transfer_review").count() == 1
    assert app_module.AdminActivity.query.filter_by(activity_type="ownership_claim_review").count() == 1


def test_admin_ownership_permission_codes_exist_for_both_roles(app_module):
    for role in ("admin", "superadmin"):
        assert "admin.ownership.view" in app_module.ADMIN_ROLE_PERMISSIONS[role]
        assert "admin.ownership.manage" in app_module.ADMIN_ROLE_PERMISSIONS[role]


# ===========================================================================
# Account conversion safeguards
# ===========================================================================
def test_vendor_downgrade_is_blocked_while_governed_dependencies_exist(app_module, db_session, plan, normal_user):
    vendor = _user(app_module, db_session, plan, "vendor-z@example.com", account_type="BUSINESS_VENDOR", limit=5)
    customer = _user(app_module, db_session, plan, "customer-z@example.com", limit=5)

    # An INDIVIDUAL account is never blocked by this rule.
    assert app_module.can_convert_to_individual(customer) == (True, None)
    assert app_module.can_convert_to_individual(vendor)[0] is True

    managed = _project(app_module, db_session, customer, creator=vendor, vendor=vendor)
    db_session.commit()
    ok, reason = app_module.can_convert_to_individual(vendor)
    assert ok is False and "managing vendor" in reason

    managed.manager_vendor_user_id = None
    db_session.commit()
    own_project = _project(app_module, db_session, vendor, name="Vendor own")
    transfer = app_module.initiate_project_ownership_transfer(own_project, vendor, customer)
    db_session.commit()
    ok, reason = app_module.can_convert_to_individual(vendor)
    assert ok is False and "transfer" in reason

    app_module.cancel_project_ownership_transfer(transfer, acting_user=vendor)
    db_session.commit()
    claim = app_module.create_project_ownership_claim(own_project, customer)
    db_session.commit()
    ok, reason = app_module.can_convert_to_individual(vendor)
    assert ok is False and "review request" in reason

    app_module.cancel_project_ownership_claim(claim, customer)
    db_session.commit()
    assert app_module.can_convert_to_individual(vendor) == (True, None)
