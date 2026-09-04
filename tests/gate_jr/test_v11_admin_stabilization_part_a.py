"""Admin stabilization pass — PART A: user + admin project integrity parity
(SCANSTORY V1.1, 2026-09-02).

Human-verified defect: Create's target-image duplicate decision was only
made at full resumable-upload finalize time - correct (see the previous
pass's canonical_target_identity_check), but too late in the UX: the whole
project's bytes had already been uploaded before the rejection surfaced.

Fix: a new /create/validate-target endpoint runs the SAME canonical check
(exact-hash + ORB/homography, via canonical_target_identity_check) the
moment a crop/full-image candidate is confirmed, comparing it against every
OTHER already-confirmed candidate in the SAME creation session - before any
full project upload/processing begins. No new duplicate-detection algorithm;
this only moves WHEN the existing canonical check runs. Shared by User and
Admin Create since both render the same wizard and this route uses
_upload_identity() (the same dual-identity pattern the resumable upload
session routes already use).

Separate finding this pass: Admin has NO edit capability at all for
admin-owned projects (no Add/Replace Target/Video routes or templates exist)
- confirmed by direct route/template search. Admin Edit parity requirements
in the brief do not apply to a feature that does not exist in the product;
this is reported explicitly rather than inventing new admin routes (which
would be a new feature, out of scope for a stabilization pass).

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_admin_stabilization_part_a.py -q
"""
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw


def _textured_jpeg_bytes(seed=0, size=(400, 400), quality=95):
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


def _cropped_variant_bytes(original_bytes, margin=6):
    img = Image.open(BytesIO(original_bytes)).convert("RGB")
    w, h = img.size
    out = BytesIO()
    img.crop((margin, margin, w - margin, h - margin)).resize((w, h)).save(out, format="JPEG", quality=90)
    return out.getvalue()


def _post_validate(client, candidate_bytes, siblings=(), experience_type="image_video"):
    data = {
        "experience_type": experience_type,
        "candidate_image": (BytesIO(candidate_bytes), "candidate.jpg"),
    }
    if siblings:
        data["sibling_candidates"] = [(BytesIO(b), f"sib-{i}.jpg") for i, b in enumerate(siblings)]
    return client.post("/create/validate-target", data=data, content_type="multipart/form-data")


# ===========================================================================
# Early validation endpoint — no project/pair ever created, User session
# ===========================================================================

def test_early_validation_unique_candidate_with_no_siblings(client, login_user):
    image = _textured_jpeg_bytes(seed=1)
    resp = _post_validate(client, image)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


def test_early_validation_catches_exact_duplicate_sibling_before_any_project_exists(
    client, app_module, login_user
):
    image = _textured_jpeg_bytes(seed=2)
    resp = _post_validate(client, image, siblings=[image])
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["verdict"] == "CONFLICT_SIBLING_CANDIDATE"
    assert body["conflict_label"] == "0"
    # The whole point of this fix: this decision happens with ZERO project
    # creation - nothing was ever persisted to reject.
    assert app_module.Project.query.count() == 0
    assert app_module.ProjectPair.query.count() == 0


def test_early_validation_catches_roi_shifted_sibling_before_any_project_exists(
    client, app_module, login_user
):
    """THE reported bug, at the earliest possible point: Pair 2 selects the
    SAME underlying target as Pair 1 with a slightly different crop, before
    the crop-confirm step even finishes - not just at finalize time."""
    original = _textured_jpeg_bytes(seed=11)
    recropped = _cropped_variant_bytes(original, margin=6)
    assert recropped != original, "test setup must actually change the bytes"

    resp = _post_validate(client, recropped, siblings=[original])
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["verdict"] == "CONFLICT_SIBLING_CANDIDATE"
    assert app_module.Project.query.count() == 0


def test_early_validation_allows_genuinely_different_targets(client, app_module, login_user):
    image_a = _textured_jpeg_bytes(seed=21)
    image_b = _textured_jpeg_bytes(seed=22)
    resp = _post_validate(client, image_b, siblings=[image_a])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


def test_early_validation_direct_qr_has_no_target_logic(client, login_user):
    """Section 20 of the prior audit, still true here: Direct QR must never
    gain image/ROI checks."""
    image = _textured_jpeg_bytes(seed=5)
    resp = _post_validate(client, image, siblings=[image], experience_type="direct_qr")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


# ===========================================================================
# Admin parity — same endpoint, same wizard, same result
# ===========================================================================

def test_early_validation_works_identically_for_admin_session(client, app_module, login_admin):
    """Admin Create renders the exact same user_create_project.html wizard
    (admin_create_project_page, app.py) - this route must accept an admin
    session via the same _upload_identity() dual-lookup the resumable
    upload session routes already use, not just a user session.

    seed=11 at the default (400, 400) size: the same combination proven
    (elsewhere in this pass and in test_v11_canonical_target_identity_fix.py)
    to reliably produce enough well-distributed ORB keypoints to pass the
    RANSAC-homography inlier ratio check after a crop. Confirmed directly
    that neither a different seed NOR a larger synthetic image helps here -
    a larger image actually produces FEWER good matches once ORB's own
    internal max-dimension downscale interacts with this fixed-cell-size
    checkerboard pattern - so admin's skipped standardization needs no
    special-cased fixture; the same proven candidate works unchanged."""
    original = _textured_jpeg_bytes(seed=11)
    recropped = _cropped_variant_bytes(original, margin=6)
    resp = _post_validate(client, recropped, siblings=[original])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "CONFLICT_SIBLING_CANDIDATE"


def test_early_validation_rejects_unauthenticated_request(client):
    image = _textured_jpeg_bytes(seed=41)
    resp = _post_validate(client, image)
    assert resp.status_code == 401


# ===========================================================================
# Admin project-edit capability: confirmed absent, documented, not invented
# ===========================================================================

def test_admin_has_no_add_or_replace_target_video_routes():
    """Confirms the audit finding: admin-owned projects have no edit routes
    at all (no admin_add_project_pair/admin_replace_target/admin_add_pair_
    media/admin_replace_pair_media equivalents). This is reported, not
    fixed - inventing new admin routes would be a new feature, explicitly
    out of scope for this stabilization pass."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    for missing in (
        "def admin_add_project_pair(",
        "def admin_replace_target(",
        "def admin_add_pair_media(",
        "def admin_replace_pair_media(",
        "def admin_edit_project(",
    ):
        assert missing not in source, f"{missing} now exists - update this test and the Part A report"


def test_edit_add_replace_target_routes_are_shared_not_duplicated():
    """The one Edit surface that DOES exist (Add/Replace Target, Add/Replace
    Video) is genuinely shared - a single resolve_target_identity_conflict/
    canonical_target_identity_check implementation, not a second one."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert source.count("def resolve_target_identity_conflict(") == 1
    assert source.count("def canonical_target_identity_check(") == 1
    assert source.count("def find_video_duplicate(") == 1


# ===========================================================================
# Structural confirmation: frontend actually calls the new endpoint
# ===========================================================================

def test_create_wizard_calls_the_new_validate_endpoint_from_both_confirm_paths():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "/create/validate-target" in html
    assert html.count("validateTargetCandidateAgainstSiblings(") >= 3  # defined once, called from both confirm paths

    use_marker_idx = html.index("async function useCurrentMarker()")
    use_marker_block = html[use_marker_idx:html.index("\n    function drawCroppedMarkerToCanvas", use_marker_idx)]
    assert "validateTargetCandidateAgainstSiblings(" in use_marker_block
    assert "validation === null" in use_marker_block, "must have a controlled failure path, not silent fail-open"

    full_image_idx = html.index("async function setFullImageMode(")
    full_image_block = html[full_image_idx:html.index("\n    function updateMarkerControls", full_image_idx)]
    assert "validateTargetCandidateAgainstSiblings(" in full_image_block
    assert "validation === null" in full_image_block


def test_validate_endpoint_never_fails_open_on_the_backend():
    """Section 13/3: a validation failure must never silently mean 'unique'.
    The route either returns a real verdict or a 4xx error - it must not
    have a bare except-and-continue that manufactures a fake UNIQUE."""
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    idx = source.index("def create_validate_target_candidate(")
    body = source[idx:source.index("\n@app.route", idx)]
    assert "canonical_target_identity_check(" in body
    assert 'jsonify({"verdict": verdict' in body
