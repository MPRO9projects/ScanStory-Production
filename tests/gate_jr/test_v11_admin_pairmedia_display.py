"""Creator Identity / Edit Flow / Direct QR remediation pass (2026-08-29),
Phase 5 - Admin PairMedia multi-video display.

Confirmed gap (audit section 22): admin/view_project.html and
admin/project_preview.html only ever read pair.video_filename (video 1),
silently dropping videos 2+ for a multi-video target, while every
creator-facing surface already loops pair.media_items. Live-verified against
project_id=7/pair_index=0 (5 real PairMedia rows) in this worktree's QA
database - this test locks in the template-level contract with a real
Flask-rendered project fixture.
"""
from pathlib import Path


def test_admin_view_project_lists_every_pairmedia_row():
    html = Path("templates/admin/view_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "evidence_pair_media_video_route" in html
    assert "{% if pair.media_items %}" in html
    assert "{% for media in pair.media_items %}" in html
    # Legacy fallback preserved for pairs with no PairMedia rows at all.
    assert "{% elif pair.video_filename %}" in html


def test_admin_project_preview_lists_every_pairmedia_row():
    """Final pre-freeze defect audit (2026-09-01), Defect A: the hero player
    on this page previously showed only pair.video_filename (the default
    video) as a playable <video>, relegating videos 2+ to download-only
    links - "3 videos on this target" was visible as text, but only one was
    ever actually playable/previewable. Now matches the already-correct
    admin/view_project.html contract: every PairMedia row renders its own
    playable <video>, legacy fallback preserved for pairs with none."""
    html = Path("templates/admin/project_preview.html").read_text(encoding="utf-8", errors="ignore")
    assert "serve_admin_pair_media_video" in html
    assert "{% if pair.media_items %}" in html
    assert "{% for media in pair.media_items %}" in html
    # Legacy fallback preserved for pairs with no PairMedia rows at all.
    assert "{% elif pair.video_filename %}" in html


def test_admin_project_preview_route_renders_every_pairmedia_video(app_module, db_session, admin, login_admin, client):
    """Live-rendered proof (not just a source-string check) that the Admin
    Preview page actually emits one playable <video> per PairMedia row for a
    multi-video target, on an admin-owned project (this route 404s for a
    creator-owned project by design - see the owner_admin_id check in
    admin_project_preview)."""
    project = app_module.Project(name="Preview PairMedia Project", owner_admin_id=admin.id, experience_type="image_video")
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(project_id=project.id, pair_index=0, image_filename="p.jpg", video_filename="v1.mp4", marker_mode="full_image")
    db_session.add(pair)
    db_session.commit()
    media1 = app_module.PairMedia(pair_id=pair.id, video_filename="v1.mp4", sort_order=0, is_default=True)
    media2 = app_module.PairMedia(pair_id=pair.id, video_filename="v2.mp4", sort_order=1, is_default=False)
    media3 = app_module.PairMedia(pair_id=pair.id, video_filename="v3.mp4", sort_order=2, is_default=False)
    db_session.add_all([media1, media2, media3])
    db_session.commit()

    resp = client.get(f"/admin/project/{project.id}/preview")
    assert resp.status_code == 200
    assert resp.data.count(b"<video") == 3
    assert b"V1 (default)" in resp.data
    assert b"V2" in resp.data
    assert b"V3" in resp.data


def test_creator_preview_route_renders_two_pairmedia_videos(app_module, db_session, project_with_pair, login_user, client):
    """Final multi-video Preview blocker (2026-09-01), mandatory 1-pair/2-video
    case per the human QA override: creates a real 1 ProjectPair + 2 PairMedia
    state and hits the real Creator Preview route (/project/<id>/preview,
    templates/user/project_preview.html), asserting both videos are actually
    rendered as playable <video> elements, not just counted/listed as text."""
    project, pair = project_with_pair
    media1 = app_module.PairMedia(pair_id=pair.id, video_filename=pair.video_filename, sort_order=0, is_default=True)
    media2 = app_module.PairMedia(pair_id=pair.id, video_filename="extra.mp4", sort_order=1, is_default=False)
    db_session.add_all([media1, media2])
    db_session.commit()

    resp = client.get(f"/project/{project.id}/preview")
    assert resp.status_code == 200
    assert resp.data.count(b"<video") == 2
    assert b"Videos (2)" in resp.data
    assert b"Video 1" in resp.data
    assert b"Video 2" in resp.data


def test_creator_preview_route_renders_three_pairmedia_videos(app_module, db_session, project_with_pair, login_user, client):
    """Mandatory 1-pair/3-video case - catches loop/indexing assumptions the
    2-video case alone would miss."""
    project, pair = project_with_pair
    media1 = app_module.PairMedia(pair_id=pair.id, video_filename=pair.video_filename, sort_order=0, is_default=True)
    media2 = app_module.PairMedia(pair_id=pair.id, video_filename="extra1.mp4", sort_order=1, is_default=False)
    media3 = app_module.PairMedia(pair_id=pair.id, video_filename="extra2.mp4", sort_order=2, is_default=False)
    db_session.add_all([media1, media2, media3])
    db_session.commit()

    resp = client.get(f"/project/{project.id}/preview")
    assert resp.status_code == 200
    assert resp.data.count(b"<video") == 3
    assert b"Videos (3)" in resp.data
    assert b"Video 1" in resp.data
    assert b"Video 2" in resp.data
    assert b"Video 3" in resp.data


def test_admin_project_preview_route_renders_two_pairmedia_videos(app_module, db_session, admin, login_admin, client):
    """Mandatory 1-pair/2-video case for Admin Preview, mirroring the Creator
    test above for content-contract parity."""
    project = app_module.Project(name="Preview 2-Video Project", owner_admin_id=admin.id, experience_type="image_video")
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(project_id=project.id, pair_index=0, image_filename="p.jpg", video_filename="v1.mp4", marker_mode="full_image")
    db_session.add(pair)
    db_session.commit()
    media1 = app_module.PairMedia(pair_id=pair.id, video_filename="v1.mp4", sort_order=0, is_default=True)
    media2 = app_module.PairMedia(pair_id=pair.id, video_filename="v2.mp4", sort_order=1, is_default=False)
    db_session.add_all([media1, media2])
    db_session.commit()

    resp = client.get(f"/admin/project/{project.id}/preview")
    assert resp.status_code == 200
    assert resp.data.count(b"<video") == 2
    assert b"V1 (default)" in resp.data
    assert b"V2" in resp.data


def test_admin_view_project_route_renders_multi_video_pair(app_module, db_session, project_with_pair, login_admin, client):
    project, pair = project_with_pair
    media1 = app_module.PairMedia(pair_id=pair.id, video_filename=pair.video_filename, sort_order=0, is_default=True)
    media2 = app_module.PairMedia(pair_id=pair.id, video_filename="extra.mp4", sort_order=1, is_default=False)
    db_session.add_all([media1, media2])
    db_session.commit()

    resp = client.get(f"/admin/projects/{project.id}")
    assert resp.status_code == 200
    assert b"2 videos" in resp.data
    assert b"V1 (default)" in resp.data
    assert b"V2" in resp.data
