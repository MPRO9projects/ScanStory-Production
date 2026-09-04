import re
from pathlib import Path


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
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/s/{project.public_key}")

    response = client.get(f"/s/{project.public_key}")
    assert response.status_code == 200
    assert b"SCANSTORY" in response.data
    assert b"opencv.js" in response.data
    assert b"detect_init" in response.data


def test_scanner_overlay_video_is_non_interactive(client, project_with_pair):
    """Fix 1 (V1 Agent 2): the AR overlay <video> must be a pure rendering surface - no
    native controls, playsinline present, PiP/fullscreen entry points suppressed, and
    pointer-events:none set directly on #overlay (not solely relied on via inheritance
    from #overlayWrap). DOM/attribute-level only - this cannot and does not certify real
    iOS Safari touch behavior; see the real-device checklist in the audit report."""
    project, _pair = project_with_pair
    response = client.get(
        f"/scanner/{project.id}?user_id={project.owner_user_id}&user_name=Normal",
        follow_redirects=True,
    )
    html = response.data.decode("utf-8")

    overlay_start = html.index('<video id="overlay"')
    overlay_tag = html[overlay_start:html.index(">", overlay_start) + 1]
    # \b won't match between "controls" and "List" (both word chars) - this correctly
    # rejects a bare `controls` attribute while allowing `controlsList=`.
    assert not re.search(r"\bcontrols\b", overlay_tag)
    assert "playsinline" in overlay_tag
    assert "disablePictureInPicture" in overlay_tag
    assert "controlslist" in overlay_tag.lower()

    style_start = html.index("#overlay {")
    style_block = html[style_start:html.index("}", style_start)]
    assert "pointer-events: none" in style_block


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


def test_target_image_route_returns_image_content_type(client, project_with_pair):
    """Regression guard: a 200 with the wrong Content-Type (e.g. text/html
    from an error page that still happened to 200) is still a broken image
    in the browser. send_from_directory must infer image/* from the real
    file on disk, which requires write path and read path to agree on the
    same absolute directory - see the DATA_DIR fix in app.py."""
    project, pair = project_with_pair
    response = client.get(f"/image/{project.id}/{pair.pair_index}")
    assert response.status_code == 200
    assert response.content_type.startswith("image/")


def test_missing_media_file_fails_safely_not_500(client, project_with_pair):
    """A pair row can exist with no corresponding file on disk (upload
    interrupted, manual cleanup, storage reconciliation). That must 404, not
    500 - a 500 here would be a worse regression than the broken <img> this
    whole issue is about."""
    project, pair = project_with_pair
    pair.image_filename = "does_not_exist_on_disk.jpg"
    from app import db

    db.session.commit()
    response = client.get(f"/image/{project.id}/{pair.pair_index}")
    assert response.status_code == 404


def test_admin_owned_project_scanner_targets_use_admin_media_routes(client, app_module, db_session, admin, tmp_path):
    """Second, independent root cause behind the same symptom: the public
    scanner's target list used to build image/video URLs unconditionally
    from the user-side serve_image/serve_video endpoints, which never read
    ADMIN_IMAGES_DIR/ADMIN_VIDEOS_DIR. Every admin-owned project's public
    scanner page showed the "Target 1" caption with a 404'd image. Assert
    the rendered page actually points at the admin-owned routes, not just
    that app.py's source mentions them."""
    project = app_module.Project(name="Admin Owned Scanner Target", owner_admin_id=admin.id, user_project_index=1)
    db_session.add(project)
    db_session.commit()

    image_path = Path(app_module.ADMIN_IMAGES_DIR) / f"{project.id}_0.jpg"
    video_path = Path(app_module.ADMIN_VIDEOS_DIR) / f"{project.id}_0.mp4"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake admin image")
    video_path.write_bytes(b"fake admin video")

    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=image_path.name,
        video_filename=video_path.name,
        is_processed=True,
        processing_status="completed",
        feature_extraction_status="extracted",
    )
    db_session.add(pair)
    db_session.commit()

    response = client.get(f"/s/{project.public_key}")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # The target guide always embeds image_url as a static <img src> (what
    # the ticket calls "Target 1"). video_url is only embedded statically
    # for direct_qr projects - for image_video (this project's default
    # experience_type) the video is fetched dynamically after camera
    # detection, so it is correctly absent from the initial HTML and is not
    # part of this assertion.
    # Cache-bust fix (master consolidated stabilization pass): the initial
    # preload now carries the same ?v= version signal the matched-detection
    # response and media[] array already did, so the URL is no longer a bare
    # path - assert the route/pair-index prefix, not an exact string.
    assert f'src="/admin/image/{project.id}/0?v=' in html
    # "/admin/image/.../0" contains "/image/.../0" as a literal substring, so
    # the negative check must anchor on the un-prefixed src attribute form,
    # not a bare "not in" on the shorter path.
    assert f'src="/image/{project.id}/0"' not in html

    image_response = client.get(f"/admin/image/{project.id}/0")
    assert image_response.status_code == 200
    assert image_response.content_type.startswith("image/")


def test_mismatched_upload_counts_rejected(client, login_user):
    response = client.post("/upload", data={}, follow_redirects=False)
    assert response.status_code == 302
