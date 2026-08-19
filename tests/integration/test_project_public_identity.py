from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


def _make_user(app_module, db_session, email, *, limit=5, used=0):
    user = app_module.User(
        email=email,
        first_name=email.split("@")[0],
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=limit,
        subscribed_scan_limit=100,
        projects_used=used,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_live_project(app_module, db_session, owner, *, index=1):
    project = app_module.Project(
        name=f"Public Identity {index}",
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        current_owner_user_id=owner.id,
        user_project_index=index,
        scanner_url="/legacy-placeholder",
        qr_code_filename=f"project_public_{index}.png",
        qr_code_path=f"/qr/project_public_{index}.png",
        is_active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def test_project_receives_unique_opaque_public_key(app_module, db_session, normal_user):
    first = _make_live_project(app_module, db_session, normal_user, index=1)
    second = _make_live_project(app_module, db_session, normal_user, index=2)

    assert first.public_key.startswith("prj_")
    assert second.public_key.startswith("prj_")
    assert first.public_key != second.public_key
    assert first.public_key != str(first.id)
    assert first.public_key != f"prj_{first.id}"


def test_project_public_key_is_immutable(app_module, db_session, normal_user):
    project = _make_live_project(app_module, db_session, normal_user)
    original = project.public_key

    project.public_key = f"{original}_changed"
    with pytest.raises(ValueError):
        db_session.commit()
    db_session.rollback()

    assert app_module.Project.query.get(project.id).public_key == original


def test_public_scanner_route_resolves_by_public_key(app_module, client, db_session, normal_user):
    project = _make_live_project(app_module, db_session, normal_user)

    response = client.get(f"/s/{project.public_key}")

    assert response.status_code == 200
    assert project.name.encode() in response.data


def test_public_scanner_route_rejects_invalid_or_unknown_key(app_module, client):
    assert client.get("/s/not-a-real-key").status_code == 404
    assert client.get("/s/bad$key").status_code == 404


def test_legacy_scanner_route_redirects_available_project_to_public_key(app_module, client, db_session, normal_user):
    project = _make_live_project(app_module, db_session, normal_user)

    response = client.get(f"/scanner/{project.id}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/s/{project.public_key}")


def test_legacy_scanner_route_preserves_unavailable_behavior(app_module, client, db_session, normal_user):
    project = _make_live_project(app_module, db_session, normal_user)
    project.is_active = False
    db_session.commit()

    response = client.get(f"/scanner/{project.id}", follow_redirects=False)

    assert response.status_code != 302
    assert response.status_code in {403, 404}


def test_canonical_qr_url_contains_no_owner_identity(app_module, db_session, normal_user):
    project = _make_live_project(app_module, db_session, normal_user)

    with app_module.app.test_request_context(base_url="https://scan.example.test"):
        scanner_url = app_module._canonical_public_scanner_url(project)

    assert f"/s/{project.public_key}" in scanner_url
    assert f"/scanner/{project.id}" not in scanner_url
    assert "user_id=" not in scanner_url
    assert "user_name=" not in scanner_url
    assert "admin_id=" not in scanner_url
    assert "admin_name=" not in scanner_url


def test_transfer_preserves_public_identity_and_scanner_url(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "public-transfer@example.com", limit=5, used=0)
    project = _make_live_project(app_module, db_session, normal_user, index=4)
    with app_module.app.test_request_context(base_url="https://scan.example.test"):
        project.scanner_url = app_module._canonical_public_scanner_url(project)
    normal_user.projects_used = 1
    db_session.commit()
    public_key = project.public_key
    scanner_url = project.scanner_url

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    db_session.commit()
    app_module.accept_project_ownership_transfer(transfer, recipient)
    db_session.commit()

    assert project.public_key == public_key
    assert project.scanner_url == scanner_url
    assert f"/s/{public_key}" in project.scanner_url
    assert project.owner_user_id == recipient.id
    assert project.created_by_user_id == normal_user.id


def test_transferred_in_project_does_not_poison_recipient_numbering(app_module, db_session, normal_user):
    recipient = _make_user(app_module, db_session, "recipient-numbering@example.com", limit=5, used=0)
    transferred = _make_live_project(app_module, db_session, normal_user, index=4)
    normal_user.projects_used = 1
    db_session.commit()

    transfer = app_module.initiate_project_ownership_transfer(transferred, normal_user, recipient)
    db_session.commit()
    app_module.accept_project_ownership_transfer(transfer, recipient)
    db_session.commit()

    assert app_module.get_project_display_number(transferred) != 4
    assert app_module._next_user_project_index(recipient.id) == 1
