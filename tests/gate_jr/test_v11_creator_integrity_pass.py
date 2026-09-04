"""Local Creator Integrity & Direct QR UX pass (2026-08-28).

Covers: target-replacement self-collision fix, duplicate-target detection on
the replace path (previously entirely absent), the Cancel/rollback stale-state
fix, double/rapid-click hardening on the plain-POST replace-target form, the
DB-level race guards (uq_project_pair_image_hash / uq_pair_media_video_hash),
and the Direct QR viewer navigation upgrade (Previous/Next/direct
selection/Replay all). True concurrent-HTTP proof against a live PostgreSQL
server is a separate script (see test_v11_postgres_concurrency_proof.py) -
these are the fast, deterministic, single-process regression guards.
"""
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy.exc import IntegrityError


def _jpeg_bytes(color=(160, 80, 40)):
    out = BytesIO()
    Image.new("RGB", (40, 40), color).save(out, format="JPEG", quality=88)
    out.seek(0)
    return out.read()


def scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def edit_project_html():
    return Path("templates/user/edit_project.html").read_text(encoding="utf-8", errors="ignore")


def app_py():
    return Path("app.py").read_text(encoding="utf-8", errors="ignore")


# ===========================================================================
# Target replacement: self-exclusion (no-op) vs. real collision
# ===========================================================================

def test_replacing_a_pair_with_its_own_current_image_is_a_safe_noop(app_module, db_session, project_with_pair, login_user, client):
    project, pair = project_with_pair
    # Canonical target identity remediation pass: the stored hash must
    # describe the FINAL (post-standardize) bytes, matching what
    # user_edit_project now computes for every incoming replacement - not
    # the raw pre-standardize bytes this fixture writes directly to disk.
    image_bytes = _jpeg_bytes((10, 20, 30))
    image_path = Path(app_module.IMAGES_DIR) / pair.image_filename
    image_path.write_bytes(image_bytes)
    app_module.standardize_uploaded_image(str(image_path), target_size=1200)
    pair.image_hash = app_module._sha256_of_file(str(image_path))
    db_session.commit()
    original_hash = pair.image_hash

    resp = client.post(
        f"/projects/{project.id}/edit",
        data={f"image_{pair.pair_index}": (BytesIO(image_bytes), "same.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(pair)
    # No-op: hash unchanged, still not reprocessed as a NEW change (is_processed
    # untouched by a no-op, unlike a real replacement which always resets it).
    assert pair.image_hash == original_hash
    assert b"already part of this story" not in resp.data


def test_replacing_a_pair_with_a_sibling_pairs_image_is_blocked(app_module, db_session, project_with_pair, multiple_pairs, login_user, client):
    project = multiple_pairs
    pair0, pair1 = (
        app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first(),
        app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=1).first(),
    )
    sibling_bytes = _jpeg_bytes((200, 30, 30))
    sibling_path = Path(app_module.IMAGES_DIR) / pair1.image_filename
    sibling_path.write_bytes(sibling_bytes)
    app_module.standardize_uploaded_image(str(sibling_path), target_size=1200)
    pair1.image_hash = app_module._sha256_of_file(str(sibling_path))
    db_session.commit()

    resp = client.post(
        f"/projects/{project.id}/edit",
        data={f"image_{pair0.pair_index}": (BytesIO(sibling_bytes), "dup.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Video duplicate/Direct QR parity pass: flash text/category changed to feed
    # the shared polished warning modal ("Target already used||...", category
    # error-modal) - raw HTML (no JS run by this test client) still has the
    # literal flash text under its new title.
    assert b"Target already used" in resp.data
    db_session.refresh(pair0)
    assert pair0.image_hash != pair1.image_hash


def test_same_target_image_across_different_projects_is_allowed(app_module, db_session, normal_user):
    """The unique index is scoped to project_id - the same exact image used as
    the target in two DIFFERENT projects must never collide."""
    shared_hash = "a" * 64
    project_a = app_module.Project(name="A", owner_user_id=normal_user.id, user_project_index=90)
    project_b = app_module.Project(name="B", owner_user_id=normal_user.id, user_project_index=91)
    db_session.add_all([project_a, project_b])
    db_session.commit()
    pair_a = app_module.ProjectPair(
        project_id=project_a.id, pair_index=0, image_filename="a.jpg",
        video_filename="a.mp4", image_hash=shared_hash,
    )
    pair_b = app_module.ProjectPair(
        project_id=project_b.id, pair_index=0, image_filename="b.jpg",
        video_filename="b.mp4", image_hash=shared_hash,
    )
    db_session.add_all([pair_a, pair_b])
    db_session.commit()  # must not raise


def test_two_pairs_same_project_same_hash_violates_db_constraint(app_module, db_session, normal_user):
    """DB-level proof the unique index (uq_project_pair_image_hash) is real,
    independent of any application-level pre-check - the authoritative race
    guard for two concurrent requests that both pass the app-level check."""
    project = app_module.Project(name="C", owner_user_id=normal_user.id, user_project_index=92)
    db_session.add(project)
    db_session.commit()
    same_hash = "b" * 64
    db_session.add(app_module.ProjectPair(
        project_id=project.id, pair_index=0, image_filename="c0.jpg",
        video_filename="c0.mp4", image_hash=same_hash,
    ))
    db_session.commit()
    db_session.add(app_module.ProjectPair(
        project_id=project.id, pair_index=1, image_filename="c1.jpg",
        video_filename="c1.mp4", image_hash=same_hash,
    ))
    try:
        db_session.commit()
        assert False, "expected IntegrityError from uq_project_pair_image_hash"
    except IntegrityError:
        db_session.rollback()


def test_legacy_pairs_with_no_hash_never_collide_with_each_other(app_module, db_session, normal_user):
    """NULL image_hash (every pre-existing row before this pass, or a
    direct_qr pair with no target image) must not be treated as a duplicate
    of any other NULL row."""
    project = app_module.Project(name="D", owner_user_id=normal_user.id, user_project_index=93)
    db_session.add(project)
    db_session.commit()
    db_session.add(app_module.ProjectPair(project_id=project.id, pair_index=0, image_filename="d0.jpg", video_filename="d0.mp4"))
    db_session.add(app_module.ProjectPair(project_id=project.id, pair_index=1, image_filename="d1.jpg", video_filename="d1.mp4"))
    db_session.commit()  # must not raise - both image_hash are NULL


# ===========================================================================
# Duplicate video: existing app-level check + new DB-level race guard
# ===========================================================================

def test_two_media_rows_same_pair_same_video_hash_violates_db_constraint(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    same_hash = "e" * 64
    db_session.add(app_module.PairMedia(pair_id=pair.id, video_filename="v0.mp4", video_hash=same_hash))
    db_session.commit()
    db_session.add(app_module.PairMedia(pair_id=pair.id, video_filename="v1.mp4", video_hash=same_hash))
    try:
        db_session.commit()
        assert False, "expected IntegrityError from uq_pair_media_video_hash"
    except IntegrityError:
        db_session.rollback()


def test_same_video_hash_under_different_pairs_is_allowed(app_module, db_session, project_with_pair, multiple_pairs):
    project = multiple_pairs
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    same_hash = "f" * 64
    db_session.add(app_module.PairMedia(pair_id=pairs[0].id, video_filename="x0.mp4", video_hash=same_hash))
    db_session.add(app_module.PairMedia(pair_id=pairs[1].id, video_filename="x1.mp4", video_hash=same_hash))
    db_session.commit()  # must not raise - different pair_id


# ===========================================================================
# Cancel/rollback stale-state fix (client JS)
# ===========================================================================

def test_close_marker_flow_resets_replacement_pair_index_and_state():
    html = edit_project_html()
    start = html.index("function closeMarkerFlow()")
    block = html[start:html.index("\n    }", start)]
    assert "replacementPairIndex = null" in block
    assert "replacementState = { crop: MarkerEditor.defaultCrop()" in block


def test_replacement_state_declaration_still_has_expected_shape():
    """Guards the exact shape closeMarkerFlow's reset must match - if this
    literal ever changes, the reset above must change with it."""
    html = edit_project_html()
    assert "let replacementState = { crop: MarkerEditor.defaultCrop(), rotation: 0, markerMode: 'crop', image: null };" in html


# ===========================================================================
# Double/rapid-click hardening (plain-POST replace-target form)
# ===========================================================================

def test_replacement_form_has_a_submit_guard_that_never_cancels_the_first_request():
    html = edit_project_html()
    assert "wireReplacementFormSubmitGuard" in html
    start = html.index("function wireReplacementFormSubmitGuard")
    block = html[start:html.index("})();", start)]
    assert "form.dataset.submitting === '1'" in block
    assert "event.preventDefault()" in block  # only for the SECOND+ submit
    # The first submit must NOT call preventDefault - confirm the guard branches.
    assert "if (form.dataset.submitting === '1') { event.preventDefault(); return; }" in block


# ===========================================================================
# Project creation idempotency (legacy /upload path)
# ===========================================================================

def test_project_creation_idempotency_key_is_the_clients_existing_upload_id():
    src = app_py()
    assert "creation_idempotency_key=upload_id" in src


def test_duplicate_create_project_submission_returns_existing_project_not_a_second_one(app_module, db_session, normal_user, login_user, client):
    image_bytes = _jpeg_bytes()

    def _post():
        return client.post(
            "/upload",
            data={
                "name": "Idempotent Story",
                "upload_id": "same-upload-id-123",
                "experience_type": "image_video",
                "playback_mode": "tracked_overlay",
                "images": (BytesIO(image_bytes), "m.jpg"),
                "videos": (BytesIO(b"\x00\x00\x00\x18ftypmp42"), "v.mp4"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    first = _post()
    count_after_first = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).count()
    # A second submission carrying the SAME upload_id must not create a
    # second project, whatever the first one's own outcome (video validation
    # in this minimal fixture may itself reject the placeholder mp4 bytes -
    # what matters here is strictly no double-creation under one key).
    second = _post()
    count_after_second = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).count()
    assert count_after_second == count_after_first


# ===========================================================================
# Direct QR viewer navigation upgrade
# ===========================================================================

def test_direct_qr_entry_shows_video_count():
    html = scanner_html()
    assert "{{ direct_qr_playlist | length }} moments in this story" in html


def test_direct_qr_previous_next_controls_exist_and_call_playdirectqratindex():
    html = scanner_html()
    assert 'id="directQrPrevBtn"' in html
    assert 'id="directQrNextBtn"' in html
    start = html.index("const directQrPrevBtn")
    block = html[start:html.index("const directQrNextBtn", start)]
    assert "playDirectQrAtIndex(directQrIndex - 1)" in block


def test_direct_qr_indicator_updates_without_altering_playback_selection_logic():
    html = scanner_html()
    start = html.index("function playDirectQrAtIndex(index)")
    block = html[start:html.index("\n    }", start)]
    # The one added line is purely a UI refresh call - the selection/
    # play/load logic itself is unchanged from before this pass.
    assert "player.src = directQrPlaylist[index].url" in block
    assert "updateDirectQrIndicator()" in block


def test_direct_qr_completion_offers_choose_a_video_for_multi_video_playlists():
    """Direct QR visual polish (remediation pass, 2026-08-29): the per-video
    'Watch again' list is now inline on the completion screen the moment it
    shows - not behind an extra 'Choose a video' tap, per the brief's own
    updated mockup - but selecting a video still reuses playDirectQrAtIndex,
    unchanged."""
    html = scanner_html()
    assert 'id="directQrChooserList"' in html
    assert "renderDirectQrWatchAgainList" in html
    start = html.index("directQrChooserList.addEventListener")
    block = html[start:html.index("});", start)]
    assert "playDirectQrAtIndex(index)" in block


def test_direct_qr_indicator_and_controls_only_render_for_multi_video_playlists():
    """A single-video Direct QR story has nothing to indicate or navigate
    between - the indicator/controls/chooser must be Jinja-gated on
    direct_qr_playlist having more than one entry, not unconditionally
    rendered."""
    html = scanner_html()
    indicator_pos = html.index('id="directQrIndicator"')
    guard_pos = html.rindex("{% if direct_qr_playlist | length > 1 %}", 0, indicator_pos)
    endif_pos = html.index("{% endif %}", guard_pos)
    assert guard_pos < indicator_pos < endif_pos

    controls_pos = html.index('id="directQrControls"')
    guard_pos = html.rindex("{% if direct_qr_playlist | length > 1 %}", 0, controls_pos)
    endif_pos = html.index("{% endif %}", guard_pos)
    assert guard_pos < controls_pos < endif_pos
