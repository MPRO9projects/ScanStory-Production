def test_protected_projects_requires_login(client):
    response = client.get("/projects")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_project_list_for_owner(client, login_user, project_with_pair):
    response = client.get("/projects")
    assert response.status_code == 200
    assert b"Baseline Project" in response.data


def test_project_pair_persistence(app_module, project_with_pair):
    project, pair = project_with_pair
    loaded = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    assert loaded.id == pair.id
    assert loaded.image_path == f"/image/{project.id}/0"


def test_scanner_route_resolves_for_existing_project(client, project_with_pair):
    project, pair = project_with_pair
    response = client.get(f"/scanner/{project.id}?user_id={project.owner_user_id}&user_name=Normal")
    assert response.status_code == 200
    assert b"SCANSTORY" in response.data
    assert b"opencv.js" in response.data
    assert b"detect_init" in response.data


def test_scanner_invalid_project_current_behavior(client):
    response = client.get("/scanner/99999")
    assert response.status_code == 200
    assert response.data == b"Project not found"


def test_qr_route_serves_existing_file(client, project_with_pair):
    project, pair = project_with_pair
    response = client.get(project.qr_code_path)
    assert response.status_code == 200
    assert response.data == b"fake qr"


def test_image_and_video_routes_use_legacy_project_pair_identity(client, project_with_pair):
    project, pair = project_with_pair
    assert client.get(f"/image/{project.id}/{pair.pair_index}").status_code == 200
    assert client.get(f"/video/{project.id}/{pair.pair_index}").status_code == 200


def test_mismatched_upload_counts_rejected(client, login_user):
    response = client.post("/upload", data={}, follow_redirects=False)
    assert response.status_code == 302
