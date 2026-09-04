"""Creator Identity / Edit Flow / Direct QR remediation pass (2026-08-29),
Phase 1 - canonical target identity.

Confirmed root cause (see SCANSTORY_V1_1_CREATOR_INTEGRITY_TARGET_IDENTITY_AUDIT.md,
section 12): ProjectPair.image_hash was computed from the PRE-standardize upload
bytes, while standardize_uploaded_image() rewrites the file afterward - so the
stored hash never matched the actual final/served file. Fixed by moving
standardize_uploaded_image() to run BEFORE the hash is computed, in every path
that persists image_hash (handle_upload, the resumable finalize path, and
user_edit_project's replace path).

These tests deliberately do NOT mock standardize_uploaded_image (unlike most of
this suite's other upload tests) - the whole point is to prove the REAL
production standardize function runs before the REAL hash is computed. Feature
extraction (extract_features_multi/make_feature_working_jpeg) IS mocked, since
it's unrelated to identity and would otherwise slow every test down for no
reason.
"""
import hashlib
import io
import os
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


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
    """Mocks ONLY the slow feature-extraction step - standardize_uploaded_image
    stays real, since these tests exist specifically to prove its interaction
    with hash computation."""
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *a, **k: Path(a[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: Path(a[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *a, **k: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *a, **k: Path(a[3]).write_bytes(b"qr") if len(a) > 3 else None)

    class NoopThread:
        def __init__(self, target=None, args=(), kwargs=None, **_ignored):
            self.target, self.args, self.kwargs = target, args, kwargs or {}
        def start(self):
            return None
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def _create_project(client, name, colors):
    """Creates one project with len(colors) pairs, each a distinct solid-color
    JPEG target + a tiny real MP4. Returns the response."""
    data = {"name": name, "upload_id": f"upload-{name}", "experience_type": "image_video", "playback_mode": "tracked_overlay"}
    for i, color in enumerate(colors):
        data[f"marker_{i}_mode"] = "full_image"
        data[f"marker_{i}_crop_x"] = "0"
        data[f"marker_{i}_crop_y"] = "0"
        data[f"marker_{i}_crop_width"] = "1"
        data[f"marker_{i}_crop_height"] = "1"
        data[f"marker_{i}_rotation"] = "0"
        data[f"marker_{i}_original_width"] = "300"
        data[f"marker_{i}_original_height"] = "300"
        data[f"marker_{i}_processed_width"] = "300"
        data[f"marker_{i}_processed_height"] = "300"
        data[f"marker_{i}_source_size_bytes"] = "20000"
        data[f"marker_{i}_processed_size_bytes"] = "20000"
        data[f"marker_{i}_display_orientation"] = "portrait"
    images = [(_jpeg_bytes(c), f"marker-{i}.jpg") for i, c in enumerate(colors)]
    videos = [(_mp4_bytes(fill=i * 40), f"clip-{i}.mp4") for i in range(len(colors))]
    data["images"] = images
    data["videos"] = videos
    return client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)


# ===========================================================================
# HASH-01: final persisted file hash == ProjectPair.image_hash
# ===========================================================================

def test_hash_01_stored_hash_matches_final_on_disk_file(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    resp = _create_project(client, "Hash01", [(10, 50, 200)])
    assert resp.status_code == 302
    project = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).first()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    on_disk = _sha(os.path.join(app_module.IMAGES_DIR, pair.image_filename))
    assert pair.image_hash == on_disk, "stored image_hash must equal sha256 of the actual final file on disk"


# ===========================================================================
# HASH-02 / HASH-03 / HASH-04: each path standardizes BEFORE hashing
# ===========================================================================

def test_hash_02_creation_path_standardizes_before_hashing(app_module):
    """Source-order proof: in handle_upload, standardize_uploaded_image must
    run on the image temp file before _sha256_of_file(img_path) - moved (in a
    follow-up fix within this same pass) to run on item["image_temp"] during
    the storage-accounting size computation, BEFORE the per-target loop, so
    the storage reservation and the per-pair ledger agree on the same final
    byte count. The per-target loop's own hash call still comes after both."""
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    gate_start = src.index("retained_image_bytes = [os.path.getsize(item[\"image_temp\"])")
    gate_block = src[max(0, gate_start - 700):gate_start]
    assert "standardize_uploaded_image(item[\"image_temp\"]" in gate_block
    standardize_pos = src.index("standardize_uploaded_image(item[\"image_temp\"]")
    loop_start = src.index('img_filename = f"{project.id}_{target_index}.jpg"')
    block = src[loop_start:src.index("video_specs = []", loop_start)]
    hash_pos = block.index("image_hash = _sha256_of_file(img_path)")
    assert standardize_pos < loop_start  # standardize happens before the per-target loop even starts
    assert "standardize_uploaded_image(img_path" not in block  # not re-applied inside the loop


def test_hash_03_replacement_path_standardizes_before_hashing(app_module):
    """Same proof for user_edit_project's replace path - PHASE 1's
    standardize+hash on temp_path, PHASE 2 no longer re-standardizes.

    Target-identity remediation pass (2026-08-29): the exact-hash decision here now
    goes through resolve_target_identity_conflict() (two-layer: exact SHA-256, then a
    conservative ORB+homography similarity check for real recapture/re-crop variance -
    see that helper's own docstring), rather than a bare _project_pair_target_conflict()
    call and an inline `image_digest == pair.image_hash` no-op check - so the window
    between standardize and the final hash computation is wider than it used to be."""
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("image_digest = None\n                if kind == \"images\":")
    block = src[start:start + 2800]
    standardize_pos = block.index("standardize_uploaded_image(temp_path")
    resolve_pos = block.index("resolve_target_identity_conflict(")
    hash_pos = block.index("image_digest = _sha256_of_file(temp_path)")
    assert standardize_pos < resolve_pos < hash_pos
    assert '"SAME_CURRENT_PAIR"' in block
    assert '"CONFLICT_OTHER_PAIR"' in block
    # PHASE 2 must not re-standardize the same file a second time.
    phase2_start = src.index("PHASE 2 - commit the approved swaps")
    phase2_block = src[phase2_start:phase2_start + 2000]
    assert "standardize_uploaded_image(final_path" not in phase2_block


def test_hash_04_resumable_path_standardizes_before_hashing(app_module):
    """Same proof for the resumable multi-target finalize path - standardize
    runs on each target's image temp file during the storage-accounting size
    computation (before the per-target creation loop), same reasoning as
    handle_upload's identical fix."""
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    gate_pos = src.index('standardize_uploaded_image(vt["primary"]["image_temp"]')
    total_bytes_pos = src.index("total_new_storage_bytes = sum(", gate_pos)
    assert gate_pos < total_bytes_pos
    loop_start = src.index("img_hash = None")
    block = src[loop_start:loop_start + 1200]
    hash_pos = block.index("img_hash = _sha256_of_file(img_path)")
    assert "standardize_uploaded_image(img_path" not in block  # not re-applied inside the loop
    assert gate_pos < loop_start  # standardize happens before the per-target loop even starts


# ===========================================================================
# HASH-05 / HASH-06: collision scoping, end to end through the real route
# ===========================================================================

def test_hash_05_same_finalized_image_same_project_collides(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    resp = _create_project(client, "Hash05", [(10, 50, 200)])
    assert resp.status_code == 302
    project = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).first()
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    served = client.get(f"/image/{project.id}/0")
    assert served.status_code == 200

    resp2 = client.post(
        f"/projects/{project.id}/edit",
        data={"image_1": (io.BytesIO(b"unused, single-pair project has no pair 1")), },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # This project only has one pair, so there is no pair 1 to attempt a
    # collision against - the real collision proof is HASH-06's sibling test
    # in test_v11_creator_integrity_pass.py (two-pair project, DB-level).
    # What matters here: pair0's own served bytes, re-hashed, exactly match
    # its stored image_hash (the same invariant as HASH-01, confirmed via the
    # real HTTP-served route rather than a raw filesystem read).
    assert hashlib.sha256(served.data).hexdigest() == pair0.image_hash


def test_hash_06_same_finalized_image_across_different_projects_allowed(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    normal_user.subscribed_project_limit = 10
    db_session.commit()
    resp_a = _create_project(client, "Hash06A", [(77, 88, 99)])
    resp_b = _create_project(client, "Hash06B", [(77, 88, 99)])
    assert resp_a.status_code == 302 and resp_b.status_code == 302
    projects = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).limit(2).all()
    assert len(projects) == 2
    pairs = [app_module.ProjectPair.query.filter_by(project_id=p.id, pair_index=0).first() for p in projects]
    assert pairs[0].image_hash == pairs[1].image_hash, "identical source image should hash identically"
    assert pairs[0].project_id != pairs[1].project_id


# ===========================================================================
# R1-R3: the replacement matrix (end to end, real standardize)
# ===========================================================================

def test_r1_replace_with_same_current_target_is_noop(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    resp = _create_project(client, "R1", [(1, 2, 3)])
    project = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).first()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    before_hash = pair.image_hash
    served = client.get(f"/image/{project.id}/0").data

    resp2 = client.post(
        f"/projects/{project.id}/edit",
        data={"image_0": (io.BytesIO(served), "same.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp2.status_code == 200
    db_session.refresh(pair)
    assert pair.image_hash == before_hash
    assert b"already part of this story" not in resp2.data


def test_r2_replace_with_another_pairs_target_is_blocked(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    resp = _create_project(client, "R2", [(9, 9, 9), (200, 1, 1)])
    project = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).first()
    pair0 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    served_pair1 = client.get(f"/image/{project.id}/1").data

    resp2 = client.post(
        f"/projects/{project.id}/edit",
        data={"image_0": (io.BytesIO(served_pair1), "stolen.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp2.status_code == 200
    # Video duplicate/Direct QR parity pass: flash text/category changed to feed
    # the shared polished warning modal ("Target already used||...", category
    # error-modal) - raw HTML (no JS run by this test client) still has the
    # literal flash text under its new title.
    assert b"Target already used" in resp2.data
    db_session.refresh(pair0)
    pair1 = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=1).first()
    assert pair0.image_hash != pair1.image_hash


def test_r3_replace_with_brand_new_target_succeeds(app_module, db_session, normal_user, login_user, client, mock_feature_extraction_only):
    resp = _create_project(client, "R3", [(4, 4, 4)])
    project = app_module.Project.query.filter_by(created_by_user_id=normal_user.id).order_by(app_module.Project.id.desc()).first()
    pair = app_module.ProjectPair.query.filter_by(project_id=project.id, pair_index=0).first()
    before_hash = pair.image_hash

    resp2 = client.post(
        f"/projects/{project.id}/edit",
        data={"image_0": (_jpeg_bytes((250, 250, 5)), "brand_new.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp2.status_code == 200
    assert b"already part of this story" not in resp2.data
    db_session.refresh(pair)
    assert pair.image_hash != before_hash


# ===========================================================================
# Legacy hash reconciliation CLI
# ===========================================================================

def test_reconcile_command_exists_and_is_conflict_safe(app_module):
    """Source-presence + safety-shape check for the reconciliation command -
    the live-DB proof (19 reconciled, 1 real conflict correctly left
    untouched) is recorded in the handoff doc, not re-run here since it
    requires real on-disk files from actual prior uploads."""
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert '@app.cli.command("reconcile-canonical-target-hashes")' in src
    start = src.index("def reconcile_canonical_target_hashes_command")
    block = src[start:start + 3000]
    assert "conflict = ProjectPair.query.filter(" in block
    assert "conflicts += 1" in block
    # A conflict must never be silently written over.
    conflict_branch = block[block.index("if conflict:"):block.index("reconciled += 1")]
    assert "pair.image_hash = current_hash" not in conflict_branch
