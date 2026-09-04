"""Canonical cross-device duplicate identity fix — focused regression tests
(SCANSTORY V1.1, 2026-09-01).

Physical-device QA proved Create's target-image duplicate check
(_find_duplicate_target_image, exact-SHA-256-only, batch-scoped) let a
same-underlying-target-but-different-ROI candidate through, while Edit's
Add/Replace Target (resolve_target_identity_conflict, exact hash + ORB/
homography similarity) already caught it. Root cause: identity was decided
by exact bytes of the CROPPED marker, not the canonical two-layer rule Edit
already used, and Create's not-yet-persisted pairs had no way to be compared
against each other at all.

Fix: canonical_target_identity_check() (app.py) wraps the existing,
unchanged resolve_target_identity_conflict() and extends it with a
sibling_candidates comparison (same exact-hash + ORB/homography algorithm,
no new thresholds) for not-yet-persisted candidates in the same Create
batch. _finalize_assemble_and_validate() now calls this instead of the old
exact-hash-only check for target images specifically (video duplicate
checks are completely untouched - same function, same scope, same rule).

These tests exercise the real code path end to end where practical (real
resumable-upload HTTP session -> finalize), not just source-string checks.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_canonical_target_identity_fix.py -q
"""
import os
import tempfile
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw


def _textured_jpeg_bytes(seed=0, size=(400, 400), quality=95):
    """Same real, ORB-rich pattern as test_v11_target_identity_remediation.py's
    _textured_jpeg_path - a flat color has almost no real keypoints, which
    would make every similarity assertion here meaningless."""
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
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _textured_jpeg_path(path, seed=0, size=(400, 400)):
    Path(path).write_bytes(_textured_jpeg_bytes(seed=seed, size=size))


def _cropped_variant_path(src_path, dst_path, margin=5):
    """Small crop shift + JPEG re-encode - same physical content, different
    bytes, different SHA-256: the exact real-world ROI-shift scenario from
    the physical-device QA report."""
    img = Image.open(src_path).convert("RGB")
    w, h = img.size
    img.crop((margin, margin, w - margin, h - margin)).resize((w, h)).save(
        str(dst_path), format="JPEG", quality=90
    )


def _mp4_bytes(width=64, height=64, frames=5):
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
        for _ in range(frames):
            writer.write(np.zeros((height, width, 3), dtype=np.uint8))
        writer.release()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ===========================================================================
# Direct unit tests: canonical_target_identity_check's sibling comparison
# ===========================================================================

def test_sibling_exact_duplicate_is_caught(app_module, tmp_path):
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(_textured_jpeg_bytes(seed=1))
    b.write_bytes(_textured_jpeg_bytes(seed=1))  # byte-identical to a

    verdict, label, diag = app_module.canonical_target_identity_check(
        None, str(b), current_pair_id=None, sibling_candidates=[("0", str(a))]
    )
    assert verdict == "CONFLICT_SIBLING_CANDIDATE"
    assert label == "0"
    assert diag.get("layer") == 1


def test_sibling_roi_shifted_duplicate_is_caught(app_module, tmp_path):
    """THE proven physical-device bug: Pair 2 selects the SAME underlying
    target as Pair 1 but with a slightly different crop/ROI, before EITHER
    is a persisted ProjectPair row. Must still be recognized as the same
    target via the ORB/homography layer, exactly like Edit's Replace Target
    already does against persisted pairs."""
    original = tmp_path / "original.jpg"
    recropped = tmp_path / "recropped.jpg"
    # seed=11: same seed the end-to-end finalize test below uses and proves
    # reliably produces enough ORB keypoints post-crop; the synthetic
    # checkerboard+dots pattern is randomly seeded, so an arbitrary seed can
    # occasionally undershoot the (deliberately conservative) match/inlier
    # thresholds by pure texture-generation chance, not a product defect.
    _textured_jpeg_path(original, seed=11)
    _cropped_variant_path(original, recropped, margin=6)
    # Match the real pipeline exactly: every real caller (both the end-to-end
    # Create finalize path and Edit's Add/Replace Target) standardizes a
    # non-admin upload before any identity check ever sees it - skipping that
    # here would test the ORB layer against a different effective resolution
    # than production actually uses.
    app_module.standardize_uploaded_image(str(original), target_size=1200)
    app_module.standardize_uploaded_image(str(recropped), target_size=1200)

    assert app_module._sha256_of_file(str(original)) != app_module._sha256_of_file(str(recropped)), \
        "test setup must actually change the bytes, or this proves nothing"

    verdict, label, diag = app_module.canonical_target_identity_check(
        None, str(recropped), current_pair_id=None, sibling_candidates=[("0", str(original))]
    )
    assert verdict == "CONFLICT_SIBLING_CANDIDATE"
    assert label == "0"
    assert diag.get("layer") == 2


def test_sibling_genuinely_different_target_is_allowed(app_module, tmp_path):
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    _textured_jpeg_path(a, seed=1)
    _textured_jpeg_path(b, seed=2)  # different seed -> genuinely different texture

    verdict, label, diag = app_module.canonical_target_identity_check(
        None, str(b), current_pair_id=None, sibling_candidates=[("0", str(a))]
    )
    assert verdict == "UNIQUE"


def test_no_siblings_falls_through_to_persisted_check_only(app_module, tmp_path):
    """sibling_candidates=None/empty must behave exactly like
    resolve_target_identity_conflict() alone - no new behavior introduced
    when there's nothing in-batch to compare against."""
    a = tmp_path / "a.jpg"
    _textured_jpeg_path(a, seed=3)
    verdict, _label, _diag = app_module.canonical_target_identity_check(
        None, str(a), current_pair_id=None, sibling_candidates=None
    )
    assert verdict == "UNIQUE"


# ===========================================================================
# End-to-end: real resumable Create HTTP flow (finalize), the exact path
# physical-device QA exercised
# ===========================================================================

def _create_session(client, image_bytes, video_bytes, project_name="Canonical Identity Test"):
    payload = {
        "image_size": len(image_bytes),
        "video_size": len(video_bytes),
        "project_name": project_name,
        "purpose": "project_content_set",
    }
    resp = client.post("/api/uploads/sessions", json=payload)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["session"]["id"]


def _upload_all(client, session_id, blob, chunk=8192):
    offset = 0
    while offset < len(blob):
        resp = client.post(
            f"/api/uploads/sessions/{session_id}/chunk",
            data=blob[offset:offset + chunk],
            headers={"X-Chunk-Offset": str(offset)},
            content_type="application/octet-stream",
        )
        assert resp.status_code == 200, resp.get_json()
        offset = resp.get_json()["current_offset"]
    return offset


def _finalize(client, session_ids):
    return client.post("/api/uploads/projects/finalize", json={"session_ids": list(session_ids)})


def _allow_pairs(app_module, user, max_pairs=5, projects=10):
    plan = user.subscription_plan
    plan.max_pairs_per_project = max_pairs
    user.subscribed_project_limit = projects
    app_module.db.session.commit()


@pytest.fixture()
def _patched_qr(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: None)


def test_create_two_pairs_same_underlying_target_different_roi_is_rejected(
    client, app_module, login_user, _patched_qr
):
    """The exact human-reported bug, end to end: Pair 1 = P1, Pair 2 = the
    SAME P1 with a slightly shifted crop. Finalize must reject with
    DUPLICATE_TARGET_IMAGE, not silently create 2 pairs."""
    _allow_pairs(app_module, login_user)
    original = _textured_jpeg_bytes(seed=11)
    fd, original_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        Path(original_path).write_bytes(original)
        recropped_path = original_path + "_recrop.jpg"
        _cropped_variant_path(original_path, recropped_path, margin=6)
        recropped = Path(recropped_path).read_bytes()
        assert recropped != original, "test setup must actually change the bytes"

        video = _mp4_bytes()
        session_1 = _create_session(client, original, video)
        _upload_all(client, session_1, original + video)
        session_2 = _create_session(client, recropped, video)
        _upload_all(client, session_2, recropped + video)

        resp = _finalize(client, [session_1, session_2])
        body = resp.get_json()
        assert resp.status_code == 409, body
        assert body.get("code") == "DUPLICATE_TARGET_IMAGE", body
        assert app_module.Project.query.count() == 0, "a rejected finalize must create no project at all"
        assert app_module.ProjectPair.query.count() == 0
    finally:
        for p in (original_path, original_path + "_recrop.jpg"):
            try:
                os.remove(p)
            except OSError:
                pass


def test_create_two_pairs_genuinely_different_targets_both_succeed(
    client, app_module, login_user, _patched_qr
):
    """Do not overcorrect: two REAL different targets in the same batch must
    still both be created."""
    _allow_pairs(app_module, login_user)
    image_a = _textured_jpeg_bytes(seed=21)
    image_b = _textured_jpeg_bytes(seed=22)
    video = _mp4_bytes()

    session_1 = _create_session(client, image_a, video)
    _upload_all(client, session_1, image_a + video)
    session_2 = _create_session(client, image_b, video)
    _upload_all(client, session_2, image_b + video)

    resp = _finalize(client, [session_1, session_2])
    assert resp.status_code == 200, resp.get_json()
    assert app_module.ProjectPair.query.count() == 2


# ===========================================================================
# Structural confirmation: the finalize route now calls the canonical helper
# ===========================================================================

def test_finalize_uses_canonical_helper_for_target_image_not_exact_hash_only():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    idx = source.index("def _finalize_assemble_and_validate")
    end = source.index("\ndef ", idx + 10)
    body = source[idx:end]
    assert "canonical_target_identity_check(" in body
    assert "sibling_candidates=" in body


def test_video_duplicate_scope_completely_unchanged_by_this_pass():
    """Section 4 lock: video duplicate checks must still use the same
    exact-hash _find_duplicate_target_image function and scope, untouched."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    idx = source.index("def _finalize_assemble_and_validate")
    end = source.index("\ndef ", idx + 10)
    body = source[idx:end]
    assert "_find_duplicate_target_image(_video_labels)" in body
    assert "_find_duplicate_target_image(_playlist_video_labels)" in body


def test_direct_qr_has_no_target_identity_logic():
    """Section 20 lock: Direct QR must never gain image/ROI checks - only
    the existing whole-playlist video duplicate rule applies to it."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    idx = source.index('if experience_type == "direct_qr":')
    block = source[idx:idx + 800]
    assert "canonical_target_identity_check" not in block
    assert "resolve_target_identity_conflict" not in block


def test_edit_add_replace_target_still_use_the_same_unchanged_resolver():
    """Edit's own routes were already canonical (resolve_target_identity_
    conflict) - this pass must not have touched them or introduced a second
    definition."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert source.count("def resolve_target_identity_conflict(") == 1
    assert source.count("def canonical_target_identity_check(") == 1
