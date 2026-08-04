from pathlib import Path


def _login_admin(client, admin):
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id


def _project(app_module, db_session, name, owner_user=None, owner_admin=None, qr=True):
    project = app_module.Project(
        name=name,
        owner_user_id=owner_user.id if owner_user else None,
        owner_admin_id=owner_admin.id if owner_admin else None,
        scanner_url="/scanner/support-test",
        qr_code_filename=f"{name.lower().replace(' ', '-')}.png" if qr else None,
        qr_code_path=f"/qr/{name.lower().replace(' ', '-')}.png" if qr else None,
    )
    db_session.add(project)
    db_session.commit()
    project.scanner_url = f"/scanner/{project.id}"
    db_session.commit()
    return project


def _pair(app_module, db_session, project, index=0, ready=True):
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=index,
        image_filename=f"{project.id}_{index}.jpg",
        video_filename=f"{project.id}_{index}.mp4",
        image_size=1234,
        video_size=5678,
        is_processed=ready,
        processing_status="completed" if ready else "processing",
        video_processing_status="compressed" if ready else "pending",
        feature_extraction_status="extracted" if ready else "pending",
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def _scan(app_module, db_session, project, pair, user, success=True):
    scan = app_module.ScanLog(
        project_id=project.id,
        pair_id=pair.id if pair else None,
        user_id=user.id,
        scan_session_id=f"session-{project.id}-{success}",
        is_successful=success,
        counted=True,
    )
    db_session.add(scan)
    db_session.commit()
    return scan


def test_admin_projects_requires_admin_auth(client):
    response = client.get("/admin/projects")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_admin_projects_route_queries_project_directly():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = source.index('def admin_projects():')
    end = source.index('@app.route("/admin/projects/<int:project_id>"', start)
    body = source[start:end]
    assert "db.session.query(\n            Project," in body
    assert "User.query" not in body
    assert ".paginate(" in body
    assert ".order_by(Project.created_at.desc(), Project.id.desc())" in body


def test_admin_projects_displays_user_owned_project(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    project = _project(app_module, db_session, "User Owned Alpha", owner_user=normal_user)
    _pair(app_module, db_session, project)
    response = client.get("/admin/projects")
    assert response.status_code == 200
    assert b"User Owned Alpha" in response.data
    assert b"User #" in response.data
    assert normal_user.email.encode() in response.data
    assert str(project.id).encode() in response.data


def test_admin_projects_displays_admin_owned_project(client, app_module, db_session, admin, secondary_admin):
    _login_admin(client, admin)
    project = _project(app_module, db_session, "Admin Owned Beta", owner_admin=secondary_admin)
    _pair(app_module, db_session, project)
    response = client.get("/admin/projects?owner_type=admin")
    assert response.status_code == 200
    assert b"Admin Owned Beta" in response.data
    assert b"Admin #" in response.data
    assert secondary_admin.email.encode() in response.data


def test_admin_projects_exact_global_project_id_search(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    target = _project(app_module, db_session, "Find Exact ID", owner_user=normal_user)
    other = _project(app_module, db_session, "Other Project", owner_user=normal_user)
    response = client.get(f"/admin/projects?search={target.id}")
    assert response.status_code == 200
    assert b"Find Exact ID" in response.data
    assert b"Other Project" not in response.data
    assert f"/admin/projects/{target.id}".encode() in response.data
    assert f"/admin/projects/{other.id}".encode() not in response.data


def test_admin_projects_owner_email_search(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    _project(app_module, db_session, "Email Search Project", owner_user=normal_user)
    response = client.get(f"/admin/projects?search={normal_user.email}")
    assert response.status_code == 200
    assert b"Email Search Project" in response.data
    assert normal_user.email.encode() in response.data


def test_admin_projects_server_side_pagination(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    for index in range(4):
        _project(app_module, db_session, f"Paged Project {index}", owner_user=normal_user)
    response = client.get("/admin/projects?per_page=2")
    assert response.status_code == 200
    assert b"Page 1 of" in response.data
    assert b"matching projects" in response.data
    assert b"Next" in response.data


def test_admin_project_detail_displays_pairs_qr_and_scan_summary(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    project = _project(app_module, db_session, "Detail Project", owner_user=normal_user)
    pair = _pair(app_module, db_session, project)
    _scan(app_module, db_session, project, pair, normal_user, success=True)
    _scan(app_module, db_session, project, pair, normal_user, success=False)
    response = client.get(f"/admin/projects/{project.id}")
    assert response.status_code == 200
    for needle in (
        b"Global Project ID",
        b"Detail Project",
        normal_user.email.encode(),
        b"Present",
        b"2",
        b"Successful",
        b"Failed",
        pair.image_filename.encode(),
        pair.video_filename.encode(),
    ):
        assert needle in response.data
    assert str(Path(pair.image_filename).resolve()).encode() not in response.data


def test_admin_project_detail_missing_project_returns_404(client, admin):
    _login_admin(client, admin)
    response = client.get("/admin/projects/999999")
    assert response.status_code == 404


def test_admin_user_profiles_preserves_filters_and_live_project_counts(client, app_module, db_session, admin, normal_user):
    _login_admin(client, admin)
    _project(app_module, db_session, "Profile Count One", owner_user=normal_user)
    _project(app_module, db_session, "Profile Count Two", owner_user=normal_user)
    response = client.get(f"/admin/user-profiles?search={normal_user.email}")
    assert response.status_code == 200
    assert b"User Profiles" in response.data
    assert b"Projects owned by displayed users" in response.data
    assert b"2" in response.data
    assert normal_user.email.encode() in response.data
    assert b"/admin/users/" in response.data
