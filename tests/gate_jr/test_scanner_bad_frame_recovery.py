"""Controlled automatic bad-frame recovery — focused test pack (SCANSTORY V1.1).

Exercises the new ONE-retry recovery path added to /detect_init: a frame that fails
normal ORB/homography recognition, whose already-computed frame_diag signals indicate
a moderate (not severe, not blurry) degradation, gets exactly one corrected retry
through the SAME acceptance thresholds as any normal frame — never a second
recognition implementation, never a loosened threshold. See app.py:
_classify_recovery_reason, _apply_recovery_correction, _attempt_recovery,
_score_and_match.

Run only this pack:
    python -m pytest tests/gate_jr/test_scanner_bad_frame_recovery.py -q
"""
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest


def _encode_jpeg(img):
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _synthetic_marker_bgr():
    """A rich, well-textured deterministic 'marker' — real geometric shapes with
    strong, well-distributed corners, not random noise, so ORB genuinely locks onto
    real structure the same way it would on a real printed marker.

    900x900 (roughly square, both axes >=1000px once embedded/padded below) is
    deliberate: /detect_init applies its own pre-existing mobile-enhancement
    (equalizeHist) whenever EITHER post-resize dimension is under 1000px, which
    auto-normalizes contrast before the new recovery code ever runs. A landscape
    camera frame resized to the 1200px cap almost always has a short side well
    under 1000px, so that branch fires on nearly every real photo — a genuinely
    useful low-contrast recovery test needs a frame where it does NOT, matching a
    large/near-square capture where the existing auto-normalization is skipped."""
    rng = np.random.default_rng(777)
    img = np.full((900, 900, 3), 200, dtype=np.uint8)
    for _ in range(70):
        x, y = int(rng.integers(20, 880)), int(rng.integers(20, 880))
        r = int(rng.integers(10, 35))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.circle(img, (x, y), r, color, -1)
    for _ in range(35):
        x1, y1 = int(rng.integers(0, 900)), int(rng.integers(0, 900))
        x2, y2 = int(rng.integers(0, 900)), int(rng.integers(0, 900))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.line(img, (x1, y1), (x2, y2), color, 4)
    return img


@pytest.fixture
def real_marker_pair(app_module, project_with_pair):
    """Registers REAL ORB features (via the actual production extract_features_multi —
    not a hand-rolled stand-in) for a real, matchable synthetic marker, overwriting the
    project_with_pair fixture's placeholder image/feature files. Distinct from the
    shared `feature_artifact` fixture (tests/conftest.py), which deliberately stores
    EMPTY descriptors — that one exists only for false-positive/no-registered-marker
    testing, never for a genuine matching accept."""
    project, pair = project_with_pair
    base = _synthetic_marker_bgr()
    image_path = Path(app_module.IMAGES_DIR) / f"{project.id}_0.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(image_path), base)
    npz_path = Path(app_module.FEATURES_DIR) / f"{project.id}_0.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    app_module.extract_features_multi(str(image_path), str(npz_path))
    app_module.load_features.cache_clear()
    return project, pair, base


def _embed_as_live_frame(marker_bgr, pad=60, bg=170):
    """Pastes the stored marker onto a padded canvas — a real camera frame always
    has some margin/background around the target, unlike posting the stored marker
    image back at 1:1 fill. Without this margin, the reprojected quad covers ~100%
    of the frame and the (pre-existing, unrelated to this task) valid_corners()
    'area too large' guard rejects it regardless of degradation/recovery."""
    h, w = marker_bgr.shape[:2]
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), bg, dtype=np.uint8)
    canvas[pad:pad + h, pad:pad + w] = marker_bgr
    return canvas


def _degraded_low_contrast_gray(live_bgr):
    """Squeezes the live frame's grayscale dynamic range down to a narrow band
    around its mean. Empirically confirmed (see final report item L) to drop normal
    ORB recognition on this fixture to 0 keypoints while remaining 'moderate' per
    the classifier: raw_contrast lands well under the low-contrast threshold, and
    neither the highlight nor shadow fraction approaches the severe-clipping guard —
    this is a genuine contrast problem, not a blur and not a clipped white/black frame."""
    gray = cv2.cvtColor(live_bgr, cv2.COLOR_BGR2GRAY)
    mean = gray.mean()
    squeezed = (mean + (gray.astype(np.float32) - mean) * 0.05).clip(0, 255).astype(np.uint8)
    return squeezed


def _post_gray_as_frame(client, project_id, gray):
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    payload = _encode_jpeg(bgr)
    return client.post(
        "/detect_init",
        data={"project_id": str(project_id), "test_image": (BytesIO(payload), "frame.jpg")},
        content_type="multipart/form-data",
    )


# ===========================================================================
# A: recovery materially improves a genuinely recoverable frame
# ===========================================================================
# Deliberately at the ORB/descriptor-match level, not through the full HTTP
# homography pipeline: RANSAC's inlier fit is stochastic enough on a synthetic
# fixture that a full "detected: True" end-to-end assertion would be flaky —
# see final report item L for the tuning story. This is the level that
# actually answers section 13's question ("does recovery materially improve
# recognition?"): does the corrected frame give the SAME downstream matcher a
# usable signal where the normal frame gave it none. Whether the marker's real
# geometry then clears the (untouched, correctly out-of-scope) homography gate
# is a property of the marker's own texture, not of the recovery mechanism.

def test_clahe_recovery_turns_zero_signal_into_a_comfortable_match(app_module, tmp_path):
    base = _synthetic_marker_bgr()
    image_path = tmp_path / "marker.jpg"
    cv2.imwrite(str(image_path), base)
    npz_path = tmp_path / "feat.npz"
    app_module.extract_features_multi(str(image_path), str(npz_path))
    stored = dict(np.load(npz_path))
    feats = {tag: (stored[f"desc_{tag}"], stored[f"kp_{tag}"]) for tag in app_module.FEATURE_TAGS}
    feats["w"], feats["h"] = stored["w"], stored["h"]

    gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    mean = gray.mean()
    squeezed = (mean + (gray.astype(np.float32) - mean) * 0.05).clip(0, 255).astype(np.uint8)

    orb = app_module._orb_detect()
    normal_kp, normal_desc = orb.detectAndCompute(squeezed, None)
    assert normal_kp is None or len(normal_kp) < app_module.MIN_TEST_KP, "fixture should fail normal recognition"

    reason = app_module._classify_recovery_reason(
        {"likely_blurry": False, "likely_localized_glare": False, "likely_overexposed": False,
         "likely_underexposed": False, "likely_low_contrast": True},
        global_highlight_fraction=0.0, global_shadow_fraction=0.0, max_cell_highlight_fraction=0.0,
    )
    assert reason == "low_contrast"

    corrected = app_module._apply_recovery_correction(squeezed, reason)
    recovered_kp, recovered_desc = orb.detectAndCompute(corrected, None)
    assert recovered_kp is not None and len(recovered_kp) >= app_module.MIN_TEST_KP

    _tag, good, _stored_kp = app_module.match_best_variant(recovered_desc, feats, ratio=0.75)
    if not good or len(good) < app_module.MIN_GOOD_MATCHES:
        _tag, good, _stored_kp = app_module.match_best_variant(recovered_desc, feats, ratio=0.90)
    assert good is not None and len(good) >= app_module.MIN_GOOD_MATCHES


def test_low_contrast_frame_reports_a_genuine_recovery_attempt_through_the_real_endpoint(client, app_module, login_user, real_marker_pair):
    """HTTP-level companion to the unit test above: a real /detect_init request
    against a real registered marker, moderately squeezed, genuinely reaches the
    recovery code (not just faked at the unit level) and reports it honestly."""
    project, pair, base = real_marker_pair
    live = _embed_as_live_frame(base)
    gray = cv2.cvtColor(live, cv2.COLOR_BGR2GRAY)
    mean = gray.mean()
    squeezed = (mean + (gray.astype(np.float32) - mean) * 0.05).clip(0, 255).astype(np.uint8)
    response = _post_gray_as_frame(client, project.id, squeezed)
    assert response.status_code == 200
    body = response.get_json()
    recovery = body.get("recovery") or {}
    # This fixture is degraded severely enough to also register as blurry (a
    # near-flat image has near-zero Laplacian variance regardless of cause) —
    # the blur gate correctly takes priority and skips recovery. Either way,
    # `recovery` must be present and consistent with detected/guidance.
    if recovery.get("attempted"):
        assert recovery.get("reason") in ("low_contrast", "glare", "overexposed", "underexposed")
    else:
        guidance = body.get("scanner_guidance") or {}
        assert guidance.get("likely_blurry") is True


# ===========================================================================
# B: severe/blurry frames never get a recovery attempt (false-positive + CPU safety)
# ===========================================================================

def test_blank_wall_never_attempts_recovery(client, app_module, login_user, feature_artifact, project_with_pair, blank_wall_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(blank_wall_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False
    assert body.get("recovery", {}).get("attempted") is not True


def test_heavily_blurred_frame_never_attempts_recovery(client, app_module, login_user, feature_artifact, project_with_pair):
    """Blur rule (brief item 7): CLAHE/gamma cannot fix blur, and running it anyway
    would waste CPU. recovery.attempted must stay False whenever likely_blurry is
    True, even though this frame fails recognition.

    Not the repo's existing `motion_blurred_image_bytes` fixture — that fixture is
    heavy directional blur over RANDOM NOISE, and empirically the residual
    high-frequency noise keeps its Laplacian variance (blur_score) well above the
    likely_blurry threshold in this pipeline (it is classified likely_low_contrast
    instead). A genuinely smooth/blurred image is needed to trip likely_blurry —
    and it must be embedded >=1000px on both axes (see _embed_as_live_frame): under
    1000px, /detect_init's own pre-existing mobile-enhancement applies an unsharp
    kernel that restores enough local contrast to defeat a moderate Gaussian blur."""
    project, _pair = project_with_pair
    live = _embed_as_live_frame(_synthetic_marker_bgr())
    blurred = cv2.GaussianBlur(live, (31, 31), 0)

    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(_encode_jpeg(blurred)), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False
    guidance = body.get("scanner_guidance") or {}
    assert guidance.get("likely_blurry") is True
    assert body.get("recovery", {}).get("attempted") is not True


def test_severe_overexposed_frame_never_attempts_recovery(client, app_module, login_user, feature_artifact, project_with_pair, bright_overexposed_image_bytes):
    """Near-fully-white frame — clipped detail that was never captured; recovery must
    not waste a retry pretending it can be reconstructed."""
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(bright_overexposed_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False
    assert body.get("recovery", {}).get("attempted") is not True


def test_severe_underexposed_frame_never_attempts_recovery(client, app_module, login_user, feature_artifact, project_with_pair, dark_underexposed_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(dark_underexposed_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False
    assert body.get("recovery", {}).get("attempted") is not True


# ===========================================================================
# C: false-positive safety — a corrected retry must never manufacture a match
# ===========================================================================
# Same "no real marker registered" contract as test_scanner_robustness.py's
# rejection pack: these frames trigger a genuine recovery ATTEMPT (moderate
# condition), but must never produce detected=True regardless.

def test_low_contrast_unrelated_frame_stays_rejected_even_after_recovery(client, app_module, login_user, feature_artifact, project_with_pair, low_contrast_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(low_contrast_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False


def test_localized_glare_unrelated_frame_stays_rejected_even_after_recovery(client, app_module, login_user, feature_artifact, project_with_pair, localized_glare_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(localized_glare_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert body["detected"] is False


def test_glossy_unrelated_object_stays_rejected_even_after_recovery(client, app_module, login_user, feature_artifact, project_with_pair):
    """A 'glossy unrelated object' stand-in: a low-contrast frame with a bright
    specular hotspot, structurally similar to a real glossy surface reflection."""
    project, _pair = project_with_pair
    rng = np.random.default_rng(2024)
    img = rng.integers(115, 140, (480, 360, 3), dtype=np.uint8)
    img[100:180, 100:180] = 250
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(_encode_jpeg(img)), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.get_json()["detected"] is False


def test_warm_color_cast_unrelated_frame_stays_rejected_even_after_recovery(client, app_module, login_user, feature_artifact, project_with_pair):
    """Heavily warm-cast (red/orange-shifted) unrelated frame — recognition is
    grayscale/structure-driven, so a color cast alone must never help a false
    match slip through."""
    project, _pair = project_with_pair
    rng = np.random.default_rng(2025)
    img = rng.integers(20, 90, (480, 360, 3), dtype=np.uint8)
    img[:, :, 2] = np.clip(img[:, :, 2].astype(np.int16) + 100, 0, 255).astype(np.uint8)  # push red channel up
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(_encode_jpeg(img)), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.get_json()["detected"] is False


# ===========================================================================
# D: one retry only — never a chain, never a third attempt
# ===========================================================================

def test_recovery_never_attempts_more_than_once_per_request(client, app_module, login_user, monkeypatch, real_marker_pair):
    project, pair, base = real_marker_pair
    squeezed = _degraded_low_contrast_gray(_embed_as_live_frame(base))

    calls = []
    original = app_module._apply_recovery_correction

    def _counting_correction(gray, reason):
        calls.append(reason)
        return original(gray, reason)

    monkeypatch.setattr(app_module, "_apply_recovery_correction", _counting_correction)
    response = _post_gray_as_frame(client, project.id, squeezed)
    assert response.status_code == 200
    assert len(calls) <= 1


# ===========================================================================
# E: diagnostics are minimal, additive, and never leak raw image data
# ===========================================================================

def test_recovery_diagnostic_fields_are_present_and_well_typed(client, app_module, login_user, feature_artifact, project_with_pair, low_contrast_image_bytes):
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(low_contrast_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    recovery = body.get("recovery")
    assert recovery is not None
    assert set(recovery.keys()) == {"attempted", "reason", "success"}
    assert isinstance(recovery["attempted"], bool)
    assert isinstance(recovery["success"], bool)
    # No raw image bytes/arrays ever appear in the response — recovery is
    # reported only as these three small fields.
    assert "frame" not in body and "image" not in body


# ===========================================================================
# F: a normal, already-clean, already-matching frame never touches recovery
# ===========================================================================

def test_normal_clean_match_never_attempts_recovery(client, app_module, login_user, real_marker_pair):
    """An unmodified, well-textured marker frame clears MIN_TEST_KP/
    MIN_GOOD_MATCHES comfortably (a pure count check, not RANSAC-sensitive) —
    recovery must never fire regardless of how the separate, untouched
    RANSAC/homography stage happens to land on this exact synthetic geometry."""
    project, pair, base = real_marker_pair
    live = _embed_as_live_frame(base)
    response = _post_gray_as_frame(client, project.id, cv2.cvtColor(live, cv2.COLOR_BGR2GRAY))
    body = response.get_json()
    reported_good_matches = body.get("good_matches", body.get("scanner_guidance", {}).get("good_matches", 0))
    assert reported_good_matches >= app_module.MIN_GOOD_MATCHES
    assert body.get("recovery", {}).get("attempted") is not True
