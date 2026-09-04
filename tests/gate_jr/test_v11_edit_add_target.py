"""Creator Identity / Edit Flow / Direct QR remediation pass (2026-08-29),
Phase 2 - Add a new pair/content set from the Edit page.

Confirmed gap (audit section 18): no backend route existed anywhere to add a
NEW ProjectPair to an already-created project. This adds one dedicated route
(user_add_project_pair, POST /projects/<id>/pair/add) reusing the existing
_reserve_pair_slots_for_project / create_pair_media_rows / canonical
duplicate-target helpers rather than any new parallel logic.
"""
import io
import os
import tempfile

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError


def _jpeg_bytes(color, size=(300, 300)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=90)
    out.seek(0)
    return out


_MP4_CACHE = {}


def _mp4_bytes(fill=0):
    if fill not in _MP4_CACHE:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
            for _ in range(5):
                writer.write(np.full((64, 64, 3), fill, dtype=np.uint8))
            writer.release()
            with open(path, "rb") as fh:
                _MP4_CACHE[fill] = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return io.BytesIO(_MP4_CACHE[fill])


@pytest.fixture()
def mock_feature_extraction_only(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: None)

    class NoopThread:
        def __init__(self, target=None, args=(), kwargs=None, **_ignored):
            self.target, self.args, self.kwargs = target, args, kwargs or {}
        def start(self):
            return None
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _add_pair(client, project_id, color=(50, 60, 70), video_fill=10):
    return client.post(
        f"/projects/{project_id}/pair/add",
        data={
            "new_pair_image": (_jpeg_bytes(color), "new.jpg"),
            "new_pair_video": (_mp4_bytes(fill=video_fill), "new.mp4"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


# ===========================================================================
# E1: add another target to an existing single-pair project
# ===========================================================================

def test_e1_add_another_target_creates_second_pair(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()

    resp = _add_pair(client, project.id, color=(1, 2, 3))
    assert resp.status_code == 200
    assert b"already part of this story" not in resp.data

    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
    assert [p.pair_index for p in pairs] == [0, 1]
    assert pairs[1].image_hash is not None
    assert pairs[1].video_filename
    media = app_module.PairMedia.query.filter_by(pair_id=pairs[1].id).all()
    assert len(media) == 1
    assert media[0].is_default is True


# ===========================================================================
# E2: attempt to add the same target another pair already owns
# ===========================================================================

def test_e2_add_target_matching_existing_pair_is_blocked(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()
    # Write real bytes so the served route can return something to re-upload.
    # Standardize BEFORE hashing, matching what the real creation/replace
    # paths now do (this pair0 was written directly to disk in this test's
    # setup, bypassing that pipeline, so it must be reproduced by hand here
    # for the stored hash to describe the same canonical bytes a real pair
    # would have).
    path = os.path.join(app_module.IMAGES_DIR, pair0.image_filename)
    with open(path, "wb") as f:
        f.write(_jpeg_bytes((9, 9, 9)).read())
    app_module.standardize_uploaded_image(path, target_size=1200)
    pair0.image_hash = app_module._sha256_of_file(path)
    db_session.commit()

    served = client.get(f"/image/{project.id}/0").data
    resp = client.post(
        f"/projects/{project.id}/pair/add",
        data={
            "new_pair_image": (io.BytesIO(served), "stolen.jpg"),
            "new_pair_video": (_mp4_bytes(fill=20), "new.mp4"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Video duplicate/Direct QR parity pass: the flash text/category changed to
    # feed the shared polished warning modal ("Target already used||...", flash
    # category error-modal) - the raw HTML (no JS executed by this test client)
    # still contains the literal flash text, just under the new title/wording.
    assert b"Target already used" in resp.data
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 1, "no second pair should have been created"


# ===========================================================================
# E3: plan pair-limit enforcement
# ===========================================================================

def test_e3_add_target_over_plan_limit_is_blocked(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only, monkeypatch):
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 1)  # already at the limit with pair0

    resp = _add_pair(client, project.id, color=(4, 5, 6))
    assert resp.status_code == 200
    assert b"maximum" in resp.data or b"plan" in resp.data
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 1


# ===========================================================================
# E4: same video reused under a DIFFERENT unique target is allowed
# ===========================================================================

def test_e4_same_video_under_new_unique_target_is_allowed(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()
    # pair0's existing video content, reused for the NEW target's video - a
    # different target, so this must be allowed per the locked duplicate-
    # video contract (same video under different unique target -> ALLOW).
    existing_video_path = os.path.join(app_module.VIDEOS_DIR, pair0.video_filename)
    os.makedirs(os.path.dirname(existing_video_path), exist_ok=True)
    with open(existing_video_path, "wb") as f:
        f.write(_mp4_bytes(fill=77).read())

    resp = client.post(
        f"/projects/{project.id}/pair/add",
        data={
            "new_pair_image": (_jpeg_bytes((11, 22, 33)), "new.jpg"),
            "new_pair_video": (_mp4_bytes(fill=77), "reused.mp4"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"already added" not in resp.data
    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 2


# ===========================================================================
# E5: double-click -> exactly one new pair
# ===========================================================================

def test_e5_double_click_add_target_creates_exactly_one_pair(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()

    # Flask's test client serves requests one at a time (no real concurrency),
    # so "double-click" here proves the DB-level guard (uq_project_pair_image_hash)
    # rejects an identical second submission cleanly with no 500 - the live
    # concurrent-HTTP proof for genuine simultaneity is a separate script
    # (test_v11_postgres_concurrency_proof.py), same as the prior pass.
    color = (66, 66, 66)
    first = _add_pair(client, project.id, color=color, video_fill=5)
    assert first.status_code == 200
    second = _add_pair(client, project.id, color=color, video_fill=6)
    assert second.status_code == 200
    assert b"Target already used" in second.data

    pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
    assert len(pairs) == 2, "exactly one new pair, not two, from the repeated identical submission"


# ===========================================================================
# Next pair_index safety (max()+1, not count())
# ===========================================================================

def test_next_pair_index_uses_max_plus_one_not_count(app_module, db_session, project_with_pair, login_user, client, mock_feature_extraction_only):
    """A gap in pair_index (e.g. an old removed pair) must not cause a
    uq_project_pair_index collision if a naive count() were used instead."""
    project, pair0 = project_with_pair
    project.experience_type = "image_video"
    db_session.commit()
    # Simulate a gap: pair_index 0 exists, jump straight to a pair_index of 5
    # as if pairs 1-4 were removed previously.
    pair0.pair_index = 5
    db_session.commit()

    resp = _add_pair(client, project.id, color=(3, 3, 3))
    assert resp.status_code == 200
    assert b"already part of this story" not in resp.data
    new_pair = app_module.ProjectPair.query.filter(
        app_module.ProjectPair.project_id == project.id, app_module.ProjectPair.pair_index != 5
    ).first()
    assert new_pair is not None
    assert new_pair.pair_index == 6


# ===========================================================================
# Route/helper reuse and QR non-regeneration (source-level proof)
# ===========================================================================

def test_add_pair_route_reuses_existing_helpers_not_custom_logic():
    from pathlib import Path
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("def user_add_project_pair")
    block = src[start:src.index("# " + "-" * 20, start + 50) if "# " + "-" * 20 in src[start + 50:] else start + 6000]
    assert "_reserve_pair_slots_for_project(" in block
    assert "create_pair_media_rows(" in block
    # Target-identity remediation pass (2026-08-29): this route now goes through the
    # single shared resolve_target_identity_conflict() helper (which itself still
    # calls _project_pair_target_conflict() as its Layer 1 exact-hash check) rather
    # than calling _project_pair_target_conflict() directly - see that helper's own
    # docstring for why a second, similarity-based layer was added.
    assert "resolve_target_identity_conflict(" in block
    assert "_schedule_project_pair_processing(" in block
    assert "get_plan_pairs_limit(" in block
    # QR must never be touched by this route.
    assert "qr_code_filename" not in block
    assert "generate_custom_qr" not in block
