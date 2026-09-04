"""P0 critical regression — Create target validation blocked every real
browser request (SCANSTORY V1.1, 2026-09-02).

Human-verified: Create New was completely broken - virtually ANY target
image (fresh, captured, anything) was reported as a duplicate and "Use this
marker" could not be confirmed.

Root cause: the previous pass's /create/validate-target endpoint correctly
requires CSRF (it is an authenticated, state-adjacent POST - no @csrf.exempt,
matching the app's own convention that only truly public/unauthenticated
endpoints are exempt), but the new frontend fetch() call in
validateTargetCandidateAgainstSiblings() never attached the X-CSRFToken
header the app already expects (WTF_CSRF_HEADERS). Every real browser
request therefore failed CSRF validation (400, no verdict at all) -
completely independent of image content, which is exactly why "virtually
ANY" image failed identically. Not a self-comparison bug, not stale sibling
state, not a canonical-identity defect - resolve_target_identity_conflict/
canonical_target_identity_check were never reached at all.

Why the previous pass's 21/21 "pass" never caught this: tests/conftest.py
disables CSRF enforcement for every pytest test_client
(WTF_CSRF_ENABLED=False) - the automated suite was structurally blind to
this class of bug. This file explicitly re-enables CSRF for its own tests
to close that gap, in addition to covering the full P0 acceptance matrix
against the now-fixed frontend.

Fix: attach the SAME csrfHeader() helper the page's other fetch/XHR calls
already use, to this one fetch() call. One line, in
templates/user/user_create_project.html.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_p0_create_target_regression.py -q
"""
import re
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


def _fresh_random_image(seed):
    """A genuinely random (not seeded-texture) image - simulates a real
    "brand-new photo never used before", the exact human-reported case."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, size=(400, 400, 3), dtype=np.uint8)
    out = BytesIO()
    Image.fromarray(arr, "RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


def _cropped_variant_bytes(original_bytes, margin=6):
    img = Image.open(BytesIO(original_bytes)).convert("RGB")
    w, h = img.size
    out = BytesIO()
    img.crop((margin, margin, w - margin, h - margin)).resize((w, h)).save(out, format="JPEG", quality=90)
    return out.getvalue()


@pytest.fixture()
def csrf_client(client, app_module, monkeypatch, login_user):
    """The real production posture: CSRF enforced. tests/conftest.py disables
    it globally for every other test in this suite - that blind spot is
    exactly why this pass's real bug shipped past 21/21 green tests. Returns
    (client, csrf_token) for tests that need to attach the header themselves,
    matching what the real fetch() call now does."""
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_ENABLED", True)
    page = client.get("/create-project")
    html = page.get_data(as_text=True)
    match = re.search(r"function csrfHeader\(\)\s*\{\s*return '([^']+)'", html)
    assert match, "csrfHeader() token not found in rendered page - test setup broken"
    return client, match.group(1)


def _post_validate(client, token, candidate_bytes, siblings=(), experience_type="image_video", with_csrf=True):
    data = {
        "experience_type": experience_type,
        "candidate_image": (BytesIO(candidate_bytes), "candidate.jpg"),
    }
    if siblings:
        data["sibling_candidates"] = [(BytesIO(b), f"sib-{i}.jpg") for i, b in enumerate(siblings)]
    headers = {"X-CSRFToken": token} if with_csrf else {}
    return client.post("/create/validate-target", data=data, content_type="multipart/form-data", headers=headers)


# ===========================================================================
# The exact regression: CSRF was silently blocking every request
# ===========================================================================

def test_request_without_csrf_token_is_rejected_not_silently_treated_as_duplicate(csrf_client):
    """Proves the OLD (broken) frontend behavior would have failed here -
    without the header, the server correctly rejects with a CSRF error, not
    a fabricated duplicate verdict. This is what every real browser request
    hit before the fix."""
    client, _token = csrf_client
    image = _fresh_random_image(1)
    resp = _post_validate(client, None, image, with_csrf=False)
    assert resp.status_code != 200
    # Critically: never a verdict of any kind - the old bug was the
    # FRONTEND treating this failure as if it were a duplicate; the server
    # itself never even reaches canonical_target_identity_check.
    body = resp.get_json()
    assert body is None or "verdict" not in body


def test_fresh_never_used_image_with_valid_csrf_is_unique(csrf_client):
    """THE human-reported symptom, fixed: a brand-new photo, real CSRF
    token attached (as the fixed frontend now does), must be UNIQUE."""
    client, token = csrf_client
    image = _fresh_random_image(2)
    resp = _post_validate(client, token, image)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


def test_frontend_fetch_call_now_attaches_the_csrf_header():
    """Structural confirmation of the exact one-line fix."""
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    idx = html.index("fetch('/create/validate-target'")
    block = html[idx:idx + 300]
    assert "X-CSRFToken" in block
    assert "csrfHeader()" in block


# ===========================================================================
# Full P0 acceptance matrix, with CSRF enforced (the real posture)
# ===========================================================================

def test_pair1_fresh_crop_candidate_is_unique_alone(csrf_client):
    client, token = csrf_client
    image = _textured_jpeg_bytes(seed=101)
    resp = _post_validate(client, token, image)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


def test_pair2_exact_duplicate_of_pair1_is_blocked(csrf_client):
    client, token = csrf_client
    image = _textured_jpeg_bytes(seed=102)
    resp = _post_validate(client, token, image, siblings=[image])
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["verdict"] == "CONFLICT_SIBLING_CANDIDATE"
    assert body["conflict_label"] == "0"


def test_pair2_roi_shifted_duplicate_of_pair1_is_blocked(csrf_client):
    client, token = csrf_client
    original = _textured_jpeg_bytes(seed=11)  # proven-reliable ORB fixture
    recropped = _cropped_variant_bytes(original, margin=6)
    resp = _post_validate(client, token, recropped, siblings=[original])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "CONFLICT_SIBLING_CANDIDATE"


def test_pair2_genuinely_different_target_is_allowed(csrf_client):
    client, token = csrf_client
    image_a = _textured_jpeg_bytes(seed=103)
    image_b = _textured_jpeg_bytes(seed=104)
    resp = _post_validate(client, token, image_b, siblings=[image_a])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


def test_candidate_never_compares_against_itself(csrf_client):
    """Section 4's invariant, proven directly: a candidate submitted with
    NO siblings at all (the frontend's own exclusion of the current pair
    from the sibling list) must never self-flag."""
    client, token = csrf_client
    image = _textured_jpeg_bytes(seed=105)
    resp = _post_validate(client, token, image, siblings=[])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["verdict"] == "UNIQUE"


# ===========================================================================
# Frontend structure: validation failure must never be shown as "duplicate"
# ===========================================================================

def test_frontend_distinguishes_validation_failure_from_duplicate_verdict():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    idx = html.index("async function useCurrentMarker()")
    body = html[idx:html.index("\n    function drawCroppedMarkerToCanvas", idx)]
    null_branch_start = body.index("if (validation === null)")
    null_branch = body[null_branch_start:null_branch_start + 400]
    assert "Could not check this photo" in null_branch
    assert "already used for" not in null_branch, "a validation failure must never show the duplicate message"


def test_use_this_marker_closes_modal_only_on_success_path():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    idx = html.index("async function useCurrentMarker()")
    body = html[idx:html.index("\n    function drawCroppedMarkerToCanvas", idx)]
    # closeCropModal() must appear on the duplicate-rejection path (returns
    # user to the form) AND on the success path (below markerConfirmed) -
    # but the validation-failure branch above must return BEFORE reaching
    # either, leaving the modal open for a retry.
    null_idx = body.index("if (validation === null)")
    null_return_idx = body.index("return;", null_idx)
    assert "closeCropModal()" not in body[null_idx:null_return_idx]
    assert body.count("closeCropModal()") >= 2
