from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


def _make_user(app_module, db_session, email, *, account_type="INDIVIDUAL", limit=3, used=0, active=True):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        account_type=account_type,
        subscription_status="active" if active else "expired",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30) if active else datetime.utcnow() - timedelta(days=1),
        subscribed_project_limit=limit,
        subscribed_scan_limit=100,
        projects_used=used,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_project(app_module, db_session, owner, *, active=True):
    project = app_module.Project(
        name="Domain Project",
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=1,
        scanner_url="/scanner/domain",
        qr_code_filename="project_domain_main.png",
        qr_code_path="/qr/project_domain_main.png",
        is_active=active,
    )
    db_session.add(project)
    db_session.commit()
    return project


def test_account_type_default_and_business_vendor_validation(app_module, db_session):
    user = app_module.User(
        email="default-account@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()

    assert user.account_type == "INDIVIDUAL"
    vendor = _make_user(app_module, db_session, "vendor@example.com", account_type="business_vendor")
    assert vendor.account_type == "BUSINESS_VENDOR"
    assert app_module.is_business_vendor(vendor)

    with pytest.raises(ValueError):
        app_module.User(
            email="bad-type@example.com",
            password_hash=generate_password_hash("password123"),
            account_type="ORG_ADMIN",
        )


def test_project_owner_helpers_keep_owner_user_id_compatible(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "recipient@example.com")
    project = _make_project(app_module, db_session, normal_user)

    app_module.set_project_current_owner(project, recipient)
    db_session.commit()

    assert project.created_by_user_id == normal_user.id
    assert project.current_owner_user_id == recipient.id
    assert project.owner_user_id == recipient.id
    assert app_module.project_current_owner_user_id(project) == recipient.id
    assert app_module.user_can_manage_project(recipient, project)
    assert not app_module.user_can_manage_project(normal_user, project)


def test_transfer_initiation_same_owner_rejection_and_recipient_only_acceptance(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "transfer-to@example.com")
    outsider = _make_user(app_module, db_session, "outsider@example.com")
    project = _make_project(app_module, db_session, normal_user)

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    assert transfer.status == "PENDING_ACCEPTANCE"
    assert transfer.from_owner_user_id == normal_user.id
    assert transfer.to_user_id == recipient.id

    with pytest.raises(ValueError):
        app_module.initiate_project_ownership_transfer(project, normal_user, normal_user)
    with pytest.raises(PermissionError):
        app_module.accept_project_ownership_transfer(transfer, outsider)


def test_transfer_pending_capacity_moves_no_counters_or_owner(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "full-recipient@example.com", limit=1, used=1)
    project = _make_project(app_module, db_session, normal_user)
    normal_user.projects_used = 1
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    result = app_module.accept_project_ownership_transfer(transfer, recipient)
    db_session.commit()

    assert result.status == "PENDING_CAPACITY"
    assert project.current_owner_user_id == normal_user.id
    assert project.owner_user_id == normal_user.id
    assert normal_user.projects_used == 1
    assert recipient.projects_used == 1
    assert project.qr_code_path == "/qr/project_domain_main.png"


def test_successful_transfer_is_atomic_and_preserves_identity(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "capacity-ok@example.com", limit=2, used=0)
    project = _make_project(app_module, db_session, normal_user)
    project_id = project.id
    qr_path = project.qr_code_path
    normal_user.projects_used = 1
    db_session.commit()
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    app_module.accept_project_ownership_transfer(transfer, recipient)
    db_session.commit()

    assert transfer.status == "COMPLETED"
    assert project.id == project_id
    assert project.qr_code_path == qr_path
    assert project.created_by_user_id == normal_user.id
    assert project.current_owner_user_id == recipient.id
    assert project.owner_user_id == recipient.id
    assert project.manager_vendor_user_id is None
    assert normal_user.projects_used == 0
    assert recipient.projects_used == 1


def test_transfer_rolls_back_reserved_capacity_when_owner_update_fails(app_module, db_session, monkeypatch, normal_user):
    recipient = _make_user(app_module, db_session, "rollback-recipient@example.com", limit=2, used=0)
    project = _make_project(app_module, db_session, normal_user)
    normal_user.projects_used = 1
    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()

    def fail_owner_update(*args, **kwargs):
        raise RuntimeError("forced transfer failure")

    monkeypatch.setattr(app_module, "set_project_current_owner", fail_owner_update)
    with pytest.raises(RuntimeError):
        app_module.accept_project_ownership_transfer(transfer, recipient)

    db_session.rollback()
    refreshed_project = app_module.Project.query.get(project.id)
    refreshed_sender = app_module.User.query.get(normal_user.id)
    refreshed_recipient = app_module.User.query.get(recipient.id)
    assert refreshed_project.owner_user_id == normal_user.id
    assert refreshed_project.current_owner_user_id == normal_user.id
    assert refreshed_sender.projects_used == 1
    assert refreshed_recipient.projects_used == 0


def test_vendor_manager_retained_only_when_explicit(app_module, db_session):
    vendor = _make_user(app_module, db_session, "vendor-owner@example.com", account_type="BUSINESS_VENDOR", limit=3, used=1)
    customer = _make_user(app_module, db_session, "customer-owner@example.com", limit=3, used=0)
    project = _make_project(app_module, db_session, vendor)

    transfer = app_module.initiate_project_ownership_transfer(project, vendor, customer, retain_vendor_management=False)
    db_session.commit()
    app_module.accept_project_ownership_transfer(transfer, customer)
    db_session.commit()
    assert project.manager_vendor_user_id is None

    project.current_owner_user_id = vendor.id
    project.owner_user_id = vendor.id
    project.manager_vendor_user_id = None
    customer.projects_used = 0
    vendor.projects_used = 1
    transfer.status = "CANCELLED"
    db_session.commit()

    retained = app_module.initiate_project_ownership_transfer(project, vendor, customer, retain_vendor_management=True)
    db_session.commit()
    app_module.accept_project_ownership_transfer(retained, customer)
    db_session.commit()
    assert project.manager_vendor_user_id == vendor.id


def test_claim_creation_dedupe_and_admin_approval_does_not_auto_transfer(app_module, db_session, normal_user, admin):
    claimant = _make_user(app_module, db_session, "claimant@example.com")
    project = _make_project(app_module, db_session, normal_user)

    claim = app_module.create_project_ownership_claim(project, claimant, evidence_summary="receipt")
    duplicate = app_module.create_project_ownership_claim(project, claimant, evidence_summary="new")
    db_session.commit()

    assert duplicate.id == claim.id
    assert project.owner_user_id == normal_user.id
    with pytest.raises(ValueError):
        app_module.create_project_ownership_claim(project, normal_user)

    approved, transfer = app_module.approve_project_ownership_claim_by_admin(claim, admin, "owner unavailable")
    db_session.commit()

    assert approved.status == "APPROVED_BY_ADMIN"
    assert transfer.status == "PENDING_ACCEPTANCE"
    assert project.owner_user_id == normal_user.id


def test_project_service_coverage_and_owner_subscription_access(app_module, db_session, normal_user):
    covered_user = normal_user
    covered_user.subscription_status = "active"
    covered_user.subscription_expires_at = datetime.utcnow() + timedelta(days=10)
    project = _make_project(app_module, db_session, covered_user)
    db_session.commit()

    state = app_module.project_public_access_state(project)
    assert state["is_live"] is True
    assert state["coverage_source"] == "OWNER_SUBSCRIPTION"

    project.is_active = False
    db_session.commit()
    assert app_module.project_public_access_state(project)["reason"] == "inactive"

    project.is_active = True
    covered_user.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
    covered_user.subscription_status = "expired"
    db_session.commit()
    assert app_module.project_public_access_state(project)["reason"] == "no_valid_coverage"

    standalone_end = datetime.utcnow() + timedelta(days=20)
    app_module.add_project_service_coverage(
        project,
        "STANDALONE_PROJECT_RENEWAL",
        coverage_end=standalone_end,
        reason="test renewal",
    )
    db_session.commit()
    state = app_module.project_public_access_state(project)
    assert state["is_live"] is True
    assert state["coverage_source"] == "STANDALONE_PROJECT_RENEWAL"
    assert state["effective_coverage_until"] == standalone_end


def test_double_coverage_uses_longest_horizon(app_module, db_session, normal_user):
    project = _make_project(app_module, db_session, normal_user)
    normal_user.subscription_status = "active"
    normal_user.subscription_expires_at = datetime.utcnow() + timedelta(days=5)
    longer = datetime.utcnow() + timedelta(days=30)
    app_module.add_project_service_coverage(project, "ADMIN_GRANT", coverage_end=longer)
    db_session.commit()

    state = app_module.project_public_access_state(project)
    assert state["is_live"] is True
    assert state["coverage_source"] == "ADMIN_GRANT"
    assert state["effective_coverage_until"] == longer


def test_public_routes_use_centralized_availability_helper(client, app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    app_module.add_project_service_coverage(project, "LEGACY_COMPATIBILITY", source_reference="test")
    db_session.commit()

    assert client.get(f"/scanner/{project.id}").status_code == 200
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 200
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 200
    assert client.get(f"/qr/{project.qr_code_filename}").status_code == 200

    project.is_active = False
    db_session.commit()
    assert client.get(f"/scanner/{project.id}").status_code == 404
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 404
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 404
    assert client.get(f"/qr/{project.qr_code_filename}").status_code == 404
