"""V1.1 P0-2: project deletion must not destroy ownership history."""
import pytest


def _transfer(app_module, db_session, project, user, status):
    row = app_module.ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=user.id,
        from_owner_user_id=user.id,
        to_user_id=user.id,
        status=status,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _claim(app_module, db_session, project, user, status):
    row = app_module.ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=user.id,
        current_owner_user_id=user.id,
        status=status,
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
def test_delete_project_with_historical_transfer_no_longer_500s(
    app_module, db_session, project_with_pair, normal_user, status
):
    project, _ = project_with_pair
    transfer = _transfer(app_module, db_session, project, normal_user, status)
    transfer_id, project_id, project_name = transfer.id, project.id, project.name

    app_module._delete_project_files_and_rows(project)

    kept = app_module.ProjectOwnershipTransfer.query.get(transfer_id)
    assert kept is not None, "ownership history must not be cascade-deleted"
    assert kept.project_id is None
    assert kept.historical_project_id == project_id
    assert kept.historical_project_name == project_name
    assert kept.status == status
    assert app_module.Project.query.get(project_id) is None


@pytest.mark.parametrize("status", ["TRANSFER_COMPLETED", "REJECTED", "CANCELLED"])
def test_delete_project_with_historical_claim_no_longer_500s(
    app_module, db_session, project_with_pair, normal_user, status
):
    project, _ = project_with_pair
    claim = _claim(app_module, db_session, project, normal_user, status)
    claim_id, project_id, project_name = claim.id, project.id, project.name

    app_module._delete_project_files_and_rows(project)

    kept = app_module.ProjectOwnershipClaim.query.get(claim_id)
    assert kept is not None
    assert kept.project_id is None
    assert kept.historical_project_id == project_id
    assert kept.historical_project_name == project_name


@pytest.mark.parametrize("status", sorted(["PENDING_ACCEPTANCE", "PENDING_CAPACITY", "DISPUTED"]))
def test_active_transfer_blocks_permanent_delete(app_module, db_session, project_with_pair, normal_user, status):
    project, _ = project_with_pair
    _transfer(app_module, db_session, project, normal_user, status)
    project_id = project.id

    with pytest.raises(app_module.ProjectDeletionBlocked) as excinfo:
        app_module._delete_project_files_and_rows(project)

    assert "transfer" in str(excinfo.value).lower()
    db_session.rollback()
    assert app_module.Project.query.get(project_id) is not None


@pytest.mark.parametrize("status", sorted(["OPEN", "VENDOR_NOTIFIED", "PENDING_ADMIN_REVIEW", "APPROVED_BY_VENDOR"]))
def test_active_claim_blocks_permanent_delete(app_module, db_session, project_with_pair, normal_user, status):
    project, _ = project_with_pair
    _claim(app_module, db_session, project, normal_user, status)
    project_id = project.id

    with pytest.raises(app_module.ProjectDeletionBlocked):
        app_module._delete_project_files_and_rows(project)

    db_session.rollback()
    assert app_module.Project.query.get(project_id) is not None


def test_blocked_delete_leaves_media_and_storage_untouched(app_module, db_session, project_with_pair, normal_user):
    """The lifecycle guard runs BEFORE any unlink or storage credit."""
    from pathlib import Path

    project, pair = project_with_pair
    _claim(app_module, db_session, project, normal_user, "OPEN")
    image = Path(app_module.IMAGES_DIR) / pair.image_filename
    video = Path(app_module.VIDEOS_DIR) / pair.video_filename
    assert image.exists() and video.exists()

    with pytest.raises(app_module.ProjectDeletionBlocked):
        app_module._delete_project_files_and_rows(project)
    db_session.rollback()

    assert image.exists() and video.exists()
    assert app_module.ProjectPair.query.filter_by(project_id=project.id).count() == 1


def test_blocked_delete_route_returns_a_safe_message_not_a_500(
    app_module, db_session, client, project_with_pair, normal_user
):
    project, _ = project_with_pair
    _transfer(app_module, db_session, project, normal_user, "PENDING_ACCEPTANCE")
    with client.session_transaction() as sess:
        sess["user_id"] = normal_user.id

    response = client.post(f"/projects/delete/{project.id}", follow_redirects=True)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "ownership transfer in progress" in body
    assert app_module.Project.query.get(project.id) is not None


def test_delete_with_history_still_removes_media_and_pairs(app_module, db_session, project_with_pair, normal_user):
    """Media/pair deletion semantics are unchanged by the history policy."""
    from pathlib import Path

    project, pair = project_with_pair
    _transfer(app_module, db_session, project, normal_user, "COMPLETED")
    image = Path(app_module.IMAGES_DIR) / pair.image_filename
    project_id = project.id

    failures = app_module._delete_project_files_and_rows(project)

    assert failures == []
    assert not image.exists()
    assert app_module.ProjectPair.query.filter_by(project_id=project_id).count() == 0


def test_detach_ownership_history_is_idempotent(app_module, db_session, project_with_pair, normal_user):
    project, _ = project_with_pair
    transfer = _transfer(app_module, db_session, project, normal_user, "COMPLETED")
    project_id, transfer_id = project.id, transfer.id

    app_module._delete_project_files_and_rows(project)
    app_module._detach_ownership_history(project_id, "irrelevant-second-call")

    kept = app_module.ProjectOwnershipTransfer.query.get(transfer_id)
    assert kept.historical_project_id == project_id
    assert kept.historical_project_name != "irrelevant-second-call"


def test_admin_ownership_page_renders_detached_history(app_module, db_session, client, project_with_pair, normal_user, admin):
    project, _ = project_with_pair
    _transfer(app_module, db_session, project, normal_user, "COMPLETED")
    project_name = project.name
    app_module._delete_project_files_and_rows(project)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    response = client.get("/admin/ownership")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert project_name in body, "detached history must stay identifiable in admin review"
