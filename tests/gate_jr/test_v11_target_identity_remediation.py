"""Real-browser target identity remediation pass (2026-08-29).

Covers the confirmed root causes from
SCANSTORY_V1_1_REAL_BROWSER_TARGET_IDENTITY_EDIT_ROI_AUDIT.md:

  1. Double-standardization / hash drift - the async processing job must never
     mutate the canonical target file again after its hash is committed.
  2. Two-layer target-identity conflict resolution (resolve_target_identity_conflict):
     exact SHA-256 first, then a conservative ORB+homography similarity layer for
     real recapture/re-crop variance that exact hash alone cannot catch.
  3. Edit -> Add another target now shares the same marker-preparation (ROI) flow
     as Create/Replace Target (source-level proof - browser interaction itself is
     covered by the real-browser/real-phone manual test pass, not here).

True concurrent-HTTP proof against a live PostgreSQL server remains a separate
script (test_v11_postgres_concurrency_proof.py).
"""
import io
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw


def _flat_jpeg_bytes(color=(160, 80, 40), size=(40, 40)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=88)
    out.seek(0)
    return out.read()


def _textured_jpeg_path(path, seed=0, size=(400, 400)):
    """A real, ORB-rich pattern (checkerboard + diagonal + noise-like dots), not a
    flat synthetic color - flat colors have almost no real keypoints, which would
    make the Layer 2 similarity tests meaningless. seed varies the pattern so two
    calls with different seeds produce genuinely different textures."""
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", size, (20, 20, 20))
    draw = ImageDraw.Draw(img)
    cell = 20
    for gx in range(0, size[0], cell):
        for gy in range(0, size[1], cell):
            if ((gx // cell) + (gy // cell) + seed) % 2 == 0:
                draw.rectangle([gx, gy, gx + cell, gy + cell], fill=tuple(int(c) for c in rng.integers(60, 220, size=3)))
    for _ in range(120):
        x, y = int(rng.integers(0, size[0])), int(rng.integers(0, size[1]))
        r = int(rng.integers(2, 6))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(c) for c in rng.integers(0, 255, size=3)))
    img.save(str(path), format="JPEG", quality=95)


# ===========================================================================
# Phase 1: async processing must never mutate the canonical target file
# ===========================================================================

def test_process_pair_never_calls_standardize_on_canonical_image(monkeypatch):
    """Source-level proof: the async job's own function body must not call
    standardize_uploaded_image on the canonical img_path at all - the prior belief
    that a second pass was harmless/idempotent is proven false for real photos (see
    the audit's controlled fb4523a9...->ccc315e0... reproduction)."""
    src = Path("processing_operations.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("def _process_pair(")
    end = src.index("def run_processing_job(")
    block = src[start:end]
    assert "standardize_uploaded_image(img_path" not in block
    assert "make_feature_working_jpeg(img_path" in block
    assert "extract_features_multi(work_img_path" in block


def test_process_pair_does_not_touch_canonical_file_bytes(tmp_path, monkeypatch):
    """Behavioral proof, not just source-level: run the real _process_pair against a
    real on-disk canonical file and assert its bytes are bit-for-bit unchanged
    afterward - the exact regression the audit found (a second standardize pass
    silently drifting the file away from its own already-committed image_hash)."""
    import processing_operations as po

    image_dir = tmp_path / "images"
    feature_dir = tmp_path / "features"
    image_dir.mkdir()
    feature_dir.mkdir()

    class FakeApp:
        def __init__(self):
            self.IMAGES_DIR = str(image_dir)
            self.FEATURES_DIR = str(feature_dir)
            self.ADMIN_IMAGES_DIR = str(image_dir)
            self.ADMIN_FEATURES_DIR = str(feature_dir)
            self.ORB_MAX_DIM = 1200
            self.standardize_calls = []

        def standardize_uploaded_image(self, path, target_size=1200):
            self.standardize_calls.append(path)
            return True

        def make_feature_working_jpeg(self, src_path, out_path, max_dim=1200, jpeg_quality=92, marker_meta=None):
            Path(out_path).write_bytes(b"fake working jpeg")
            return out_path

        def extract_features_multi(self, image_path, save_path, max_dim=1200):
            np.savez(save_path, w=np.int32(10), h=np.int32(10))

        def _elapsed_ms(self, start):
            return 0

    class FakeProject:
        id = 1
        owner_admin_id = None

    class FakePair:
        pair_index = 0
        image_filename = "1_0.jpg"

    img_path = image_dir / "1_0.jpg"
    _textured_jpeg_path(img_path, seed=1)
    before_bytes = img_path.read_bytes()

    fake_app = FakeApp()
    result = po._process_pair(fake_app, FakeProject(), FakePair())

    after_bytes = img_path.read_bytes()
    assert before_bytes == after_bytes, "canonical target file must be byte-identical after async processing"
    assert fake_app.standardize_calls == [], "async processing must never call standardize_uploaded_image on the canonical file"
    assert result["image_standardization_duration_ms"] == 0


# ===========================================================================
# Phase 2: Edit -> Add Target shares the same ROI/marker-preparation flow
# ===========================================================================

def test_add_target_panel_opens_shared_marker_flow_not_raw_file_picker():
    """The confirmed audit finding: Add Target used to open the raw file picker
    directly (document.getElementById('new_pair_image').click()), which is why a
    real camera photo's EXIF survived all the way to the server untouched. It must
    now open the exact same marker-preparation flow Replace Target already uses."""
    html = Path("templates/user/edit_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "document.getElementById('new_pair_image').click()" not in html
    idx = html.index('id="new-pair-img-zone"')
    block = html[idx:idx + 200]
    assert "openImageSourceChooser('new')" in block


def test_confirm_replacement_marker_routes_new_target_to_add_target_input():
    """confirmReplacementMarker() must write the exported (cropped/rotated) blob onto
    new_pair_image when the flow was opened for 'new', and onto image_<index>
    otherwise - one shared export path, two possible destination inputs."""
    html = Path("templates/user/edit_project.html").read_text(encoding="utf-8", errors="ignore")
    idx = html.index("async function confirmReplacementMarker()")
    next_fn = html.index("\n    function ", idx)
    block = html[idx:next_fn]
    assert "isNewTarget" in block
    assert "'new_pair_image'" in block
    assert "new-pair-img-zone" in block


# ===========================================================================
# Phase 3: two-layer target-identity conflict resolution
# ===========================================================================

@pytest.fixture()
def two_pair_project(app_module, db_session, normal_user):
    project = app_module.Project(
        name="Identity Remediation", owner_user_id=normal_user.id, user_project_index=501,
        experience_type="image_video",
    )
    db_session.add(project)
    db_session.commit()

    os.makedirs(app_module.IMAGES_DIR, exist_ok=True)
    os.makedirs(app_module.FEATURES_DIR, exist_ok=True)

    def _make_pair(index, seed):
        img_path = os.path.join(app_module.IMAGES_DIR, f"{project.id}_{index}.jpg")
        _textured_jpeg_path(img_path, seed=seed)
        app_module.standardize_uploaded_image(img_path, target_size=1200)
        work_path = os.path.join(app_module.IMAGES_DIR, f"{project.id}_{index}_work.jpg")
        npz_path = os.path.join(app_module.FEATURES_DIR, f"{project.id}_{index}.npz")
        app_module.make_feature_working_jpeg(img_path, work_path, max_dim=app_module.ORB_MAX_DIM, jpeg_quality=92)
        app_module.extract_features_multi(work_path, npz_path, max_dim=app_module.ORB_MAX_DIM)
        os.remove(work_path)
        pair = app_module.ProjectPair(
            project_id=project.id, pair_index=index,
            image_filename=f"{project.id}_{index}.jpg", video_filename=f"{project.id}_{index}.mp4",
            image_path=f"/image/{project.id}/{index}",
            image_hash=app_module._sha256_of_file(img_path),
            is_processed=True, processing_status="completed", feature_extraction_status="extracted",
        )
        db_session.add(pair)
        db_session.commit()
        return pair

    pair0 = _make_pair(0, seed=10)
    pair1 = _make_pair(1, seed=20)
    app_module.load_features.cache_clear()
    return project, pair0, pair1


def test_d1_exact_duplicate_against_other_pair_is_conflict(app_module, two_pair_project, tmp_path):
    project, pair0, pair1 = two_pair_project
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(Path(os.path.join(app_module.IMAGES_DIR, pair0.image_filename)).read_bytes())

    verdict, conflict_pair, diag = app_module.resolve_target_identity_conflict(
        project.id, str(candidate), current_pair_id=None
    )
    assert verdict == "CONFLICT_OTHER_PAIR"
    assert conflict_pair.id == pair0.id


def test_d2_replace_with_own_current_target_is_noop(app_module, two_pair_project, tmp_path):
    project, pair0, pair1 = two_pair_project
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(Path(os.path.join(app_module.IMAGES_DIR, pair0.image_filename)).read_bytes())

    verdict, matched_pair, diag = app_module.resolve_target_identity_conflict(
        project.id, str(candidate), current_pair_id=pair0.id
    )
    assert verdict == "SAME_CURRENT_PAIR"
    assert matched_pair.id == pair0.id


def test_d3_replace_with_a_different_pairs_target_is_conflict(app_module, two_pair_project, tmp_path):
    project, pair0, pair1 = two_pair_project
    candidate = tmp_path / "candidate.jpg"
    candidate.write_bytes(Path(os.path.join(app_module.IMAGES_DIR, pair1.image_filename)).read_bytes())

    verdict, conflict_pair, diag = app_module.resolve_target_identity_conflict(
        project.id, str(candidate), current_pair_id=pair0.id
    )
    assert verdict == "CONFLICT_OTHER_PAIR"
    assert conflict_pair.id == pair1.id


def test_genuinely_different_target_is_unique(app_module, two_pair_project, tmp_path):
    """S5 (false-positive protection): a genuinely different target must never be
    blocked just because it shares a similar palette/layout with an existing one."""
    project, pair0, pair1 = two_pair_project
    candidate = tmp_path / "different.jpg"
    _textured_jpeg_path(candidate, seed=999)

    verdict, matched_pair, diag = app_module.resolve_target_identity_conflict(
        project.id, str(candidate), current_pair_id=None
    )
    assert verdict == "UNIQUE"


def test_recapture_with_small_recrop_is_still_recognized_as_same_target(app_module, two_pair_project, tmp_path):
    """S1/S2 (the audit's central Level 2 finding): a real recapture/re-crop of the
    SAME physical target changes the SHA-256 but must still be caught by the
    conservative similarity layer, not silently allowed as a fresh duplicate."""
    project, pair0, pair1 = two_pair_project
    original = Image.open(os.path.join(app_module.IMAGES_DIR, pair0.image_filename)).convert("RGB")
    w, h = original.size
    # Small crop shift + JPEG re-encode - same physical content, different bytes,
    # different SHA-256, exactly the real-world recapture scenario from the audit.
    cropped = original.crop((3, 3, w - 3, h - 3)).resize((w, h))
    candidate = tmp_path / "recaptured.jpg"
    cropped.save(str(candidate), format="JPEG", quality=90)

    candidate_hash = app_module._sha256_of_file(str(candidate))
    assert candidate_hash != pair0.image_hash, "test setup must actually change the bytes"

    verdict, matched_pair, diag = app_module.resolve_target_identity_conflict(
        project.id, str(candidate), current_pair_id=None
    )
    assert verdict == "CONFLICT_OTHER_PAIR"
    assert matched_pair.id == pair0.id
    assert diag.get("layer") == 2


def test_same_video_under_different_unique_target_still_allowed(app_module, two_pair_project):
    """Section 34 lock: the similarity guard is scoped to TARGET identity only - it
    must never be reachable from, or tighten, the separate video-duplicate rules."""
    src = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("def user_add_pair_media")
    end = src.index("def ", start + 10)
    block = src[start:end]
    assert "resolve_target_identity_conflict" not in block
    assert "_is_high_confidence_same_target" not in block
